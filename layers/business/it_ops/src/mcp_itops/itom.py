"""ITOM 平台（itom.shougang.com.cn）客户端：**预设账户 + 服务端持凭据**。

安全契约（与 Go 层 dsn_ref 同构，见 docs/architecture.md「敏感数据边界」）：
- 账号口令只写在 config/platform.env（不进版本库），AI 侧全程只流转 account_ref；
  对话里出现明文口令会进模型上下文与平台会话记录，审计也遮不住自然语言，所以不做。
- 登录换来的 token 只在进程内缓存，过期自动重登；token 不回传给调用方。
- 审计侧 passwd/token/cookie 命中敏感键会整体遮蔽（mcp_shared.audit.SENSITIVE_KEYS）。

配置（config/platform.env）：
    MCP_ITOM_URL=https://itom.shougang.com.cn/api
    MCP_ITOM_ACCOUNTS=wangxu,ops01
    MCP_ITOM_WANGXU_USER_ID=<工号>
    MCP_ITOM_WANGXU_PASSWORD=<口令>
    MCP_ITOM_WANGXU_ORG_SID=266
    MCP_ITOM_WANGXU_ORG_POSITION_SID=1154
    MCP_ITOM_WANGXU_NOTE=运维中心        # 可选，出现在 accounts 列表里帮 AI/人辨认
    # 已有长期 token、不想配口令时改这一行（配了就直接用，不再走登录）：
    # MCP_ITOM_WANGXU_TOKEN=<token>
可选覆盖：MCP_ITOM_LOGIN_PATH（默认 /auth/login/login?uid=null）、
MCP_ITOM_TOKEN_KEY（响应里 token 的键名，不配则按常见键名递归找）、
MCP_ITOM_TOKEN_HEADER（默认 Authorization）、MCP_ITOM_TOKEN_SCHEME（默认空=裸值）、
MCP_ITOM_TOKEN_TTL（秒，默认 1800）、MCP_ITOM_TIMEOUT_SECONDS（默认 30）、
MCP_ITOM_USER_AGENT / MCP_ITOM_REFERER（平台前置 WAF 挑客户端时才需要配）。
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx2

from mcp_shared.config import get as cfg
from mcp_shared.limits import check_text

DEFAULT_LOGIN_PATH = "/auth/login/login?uid=null"
# 平台常见 token 键名（大小写不敏感，递归找第一命中）；配 MCP_ITOM_TOKEN_KEY 可锁定。
# 另有一条「以 token 结尾」的兜底规则：ITOM 实际用的是 gm_auth_token 这类自定义名。
_TOKEN_KEYS = (
    "token", "accessToken", "access_token", "authToken", "auth_token",
    "sessionId", "sessionid", "sid", "ticket", "jwt", "idToken", "id_token",
)
_MAX_TEXT = 4000
# 平台前置组件会挑客户端特征，默认照抄一个桌面 Chrome UA（可用 MCP_ITOM_USER_AGENT 覆盖）
_DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
# 平台的请求/响应信封（ITOM 一律 {"reqHeader":…,"reqBody":…}）
_ENVELOPE_HEADER = "reqHeader"
_ENVELOPE_BODY = "reqBody"
# 语义只读但用 POST 发的接口（findByPage 这类分页查询）——按路径末段判断，
# 不听调用方自称，否则审批闸门就成了摆设
_READ_SEGMENTS = (
    "find", "get", "list", "query", "select", "search", "view", "detail",
    "count", "check", "export", "load", "is", "has",
)


# 错误码（转 ToolError 后以 [CODE] 前缀出现在错误文本里，AI 可据此纠正重试）
CODE_NOT_CONFIGURED = "ITOM_NOT_CONFIGURED"
CODE_INVALID_INPUT = "ITOM_INVALID_INPUT"
CODE_UNREACHABLE = "ITOM_UNREACHABLE"
CODE_HTTP_ERROR = "ITOM_HTTP_ERROR"
CODE_API_ERROR = "ITOM_API_ERROR"
CODE_AMBIGUOUS = "ITOM_AMBIGUOUS"
CODE_NOT_FOUND = "ITOM_NOT_FOUND"


class ItomError(RuntimeError):
    """配置缺失、鉴权失败、路径非法、平台返回错误——统一带 error_code 与可执行建议。

    str(e) 形如 ``[ITOM_NOT_FOUND] 没找到 …；建议 …``：AI 靠前缀里的错误码
    分类理解失败原因，靠消息里的建议自行纠正后重试。
    """

    def __init__(self, message: str, *, code: str = "ITOM_ERROR") -> None:
        self.code = code
        super().__init__(f"[{code}] {message}")


@dataclass
class Account:
    ref: str
    user_id: str | None
    password: str | None
    org_sid: str
    org_position_sid: str
    note: str
    static_token: str | None

    @property
    def can_login(self) -> bool:
        return bool(self.user_id and self.password)


@dataclass
class _Session:
    token: str | None
    cookies: dict[str, str]
    obtained_at: float
    ttl: float

    @property
    def expired(self) -> bool:
        return time.time() - self.obtained_at > self.ttl


_sessions: dict[str, _Session] = {}
_lock = threading.RLock()


def _env_suffix(ref: str) -> str:
    """account_ref → 环境变量后缀：非字母数字统一转下划线并大写。"""
    return "".join(ch if ch.isalnum() else "_" for ch in ref.strip()).upper()


def configured_refs() -> list[str]:
    raw = cfg("MCP_ITOM_ACCOUNTS") or ""
    return [r.strip() for r in raw.split(",") if r.strip()]


def account(ref: str) -> Account:
    refs = configured_refs()
    if not refs:
        raise ItomError(
            "ITOM 账户未配置：请在 config/platform.env 写 MCP_ITOM_ACCOUNTS=<引用名,逗号分隔> "
            "以及每个引用名的 MCP_ITOM_<引用名大写>_USER_ID/_PASSWORD（该文件不进版本库）。"
            "AI 侧只需要引用名，不需要也不应该出现口令。",
            code=CODE_NOT_CONFIGURED,
        )
    ref = (ref or "").strip()
    if ref in ("", "default"):
        # 模型很习惯传 "default"；单账户部署时直接落到清单里的第一个引用名，
        # 免得它在"未知引用名"和"该用哪个账号"之间反复横跳
        ref = refs[0]
    if ref not in refs:
        raise ItomError(
            f"未知的 ITOM 账户引用名 {ref!r}；可用：{', '.join(refs)}"
            "（先调 itops_list_itom_accounts 看完整清单）",
            code=CODE_NOT_FOUND,
        )
    stem = f"MCP_ITOM_{_env_suffix(ref)}_"
    acct = Account(
        ref=ref,
        user_id=cfg(stem + "USER_ID"),
        password=cfg(stem + "PASSWORD"),
        org_sid=cfg(stem + "ORG_SID") or "",
        org_position_sid=cfg(stem + "ORG_POSITION_SID") or "",
        note=cfg(stem + "NOTE") or "",
        static_token=cfg(stem + "TOKEN"),
    )
    if not acct.static_token and not acct.can_login:
        raise ItomError(
            f"账户 {ref} 缺少凭据：请配 {stem}USER_ID + {stem}PASSWORD，"
            f"或改用长期令牌方式配 {stem}TOKEN",
            code=CODE_NOT_CONFIGURED,
        )
    return acct


def accounts_summary() -> list[dict[str, Any]]:
    """给 AI 看的清单：引用名 + 备注 + 打码工号 + 会话状态，绝不含口令。"""
    out: list[dict[str, Any]] = []
    for ref in configured_refs():
        with _lock:
            sess = _sessions.get(ref)
        try:
            a = account(ref)
        except ItomError as e:
            # 在册但没配全的引用名要显式可见，否则 AI 会以为它可用然后一路报错
            out.append({"account_ref": ref, "configured": False, "reason": str(e)[:160]})
            continue
        out.append({
            "account_ref": a.ref,
            "note": a.note,
            "user_id": _mask(a.user_id or ""),
            "credential": "token" if a.static_token else "password",
            "logged_in": bool(sess and not sess.expired),
        })
    return out


def _mask(value: str) -> str:
    if len(value) <= 2:
        return value[:1] + "***"
    return value[0] + "***" + value[-1]


def _base_url() -> str:
    base = (cfg("MCP_ITOM_URL") or "").rstrip("/")
    if not base:
        raise ItomError(
            "ITOM 地址未配置：请在 config/platform.env 设 MCP_ITOM_URL"
            "（形如 https://itom.shougang.com.cn/api，含 /api 前缀）",
            code=CODE_NOT_CONFIGURED,
        )
    if not base.startswith(("http://", "https://")):
        raise ItomError(f"MCP_ITOM_URL 必须是 http(s) 地址，当前 {base!r}",
                        code=CODE_INVALID_INPUT)
    return base


def _timeout() -> float:
    try:
        return float(cfg("MCP_ITOM_TIMEOUT_SECONDS") or 30)
    except ValueError:
        return 30.0


def _platform_headers() -> dict[str, str]:
    """ITOM 是老式 jQuery 前端，后端会挑 Ajax 特征头——缺了这些常表现为 403/跳登录页。
    UA/Referer 可在配置里照抄浏览器值覆盖。"""
    headers = {
        "content-type": "application/json",
        "accept": "text/plain, */*; q=0.01",
        "x-requested-with": "XMLHttpRequest",
        "user-agent": cfg("MCP_ITOM_USER_AGENT") or _DEFAULT_UA,
    }
    ref = cfg("MCP_ITOM_REFERER")
    if ref:
        headers["referer"] = ref
    return headers


def _is_token_key(name: str, wanted: str | None) -> bool:
    k = name.strip().lower()
    if wanted:
        return k == wanted.strip().lower()
    # 覆盖 accessToken / gm_auth_token / authToken 这类命名
    return k.endswith("token") or k in {x.lower() for x in _TOKEN_KEYS}


def _find_token(payload: Any) -> tuple[str | None, str | None]:
    """在响应里找 token：返回 (值, 命中的键名)。MCP_ITOM_TOKEN_KEY 优先。

    平台返回结构未知且各模块不一致是常态，所以这里既支持显式指定键名，
    也按常见键名广度优先扫一遍嵌套结构。
    """
    wanted = (cfg("MCP_ITOM_TOKEN_KEY") or "").strip()
    queue: list[Any] = [payload]
    depth = 0
    while queue and depth < 6:
        nxt: list[Any] = []
        for node in queue:
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, str) and v.strip() and _is_token_key(str(k), wanted):
                        return v.strip(), str(k)
                    nxt.append(v)
                # 平台把业务体裹在 reqBody 里的写法
                nxt.append(node.get(_ENVELOPE_BODY))
            elif isinstance(node, list):
                nxt.extend(node[:20])
        queue = [n for n in nxt if isinstance(n, (dict, list))]
        depth += 1
    return None, None


def _send(method: str, url: str, *, params: dict[str, Any] | None,
          json_body: Any | None, headers: dict[str, str]) -> Any:
    return httpx2.request(method, url, params=params, json=json_body,
                          headers=headers, timeout=_timeout())


def _login(a: Account) -> _Session:
    url = _base_url() + (cfg("MCP_ITOM_LOGIN_PATH") or DEFAULT_LOGIN_PATH)
    body = {
        "reqHeader": {"operTitle": ""},
        "reqBody": {
            "userId": a.user_id,
            "passwd": a.password,
            "orgSid": a.org_sid,
            "orgPositionSid": a.org_position_sid,
            "operTitle": "",
        },
    }
    try:
        resp = _send("POST", url, params=None, json_body=body, headers=_platform_headers())
    except httpx2.HTTPError as e:
        raise ItomError(f"ITOM 登录请求失败（{url}）：{type(e).__name__}: {e}",
                        code=CODE_UNREACHABLE) from e
    if resp.status_code >= 400:
        raise ItomError(
            f"ITOM 登录返回 [{resp.status_code}]：{_snippet(resp.text)}"
            "（403 多为平台 WAF 挑客户端，配 MCP_ITOM_USER_AGENT/MCP_ITOM_REFERER 再试）",
            code=CODE_HTTP_ERROR,
        )
    payload = _parse_body(resp)
    token, key = _find_token(payload)
    cookies = dict(resp.cookies)
    if not token and not cookies:
        raise ItomError(
            "ITOM 登录成功但没找到 token：响应键名不在常见列表里。"
            f"实际键名={_keys_of(payload)}，cookie={list(cookies)}；"
            "请把命中的键名配成 MCP_ITOM_TOKEN_KEY",
            code=CODE_NOT_CONFIGURED,
        )
    ttl = _ttl()
    # 纯 cookie 会话（Java 平台常见）：没有 token 也能带着 cookie 调后续接口
    # 出错信息里只出现键名，绝不出现口令/token
    _last_source[a.ref] = key or "cookie"
    return _Session(token=token, cookies=cookies, obtained_at=time.time(), ttl=ttl)


_last_source: dict[str, str] = {}


def _ttl() -> float:
    try:
        return float(cfg("MCP_ITOM_TOKEN_TTL") or 1800)
    except ValueError:
        return 1800.0


def _parse_body(resp: Any) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"raw": _snippet(resp.text)}


def _snippet(text: str) -> str:
    text = (text or "").strip()
    return text[:_MAX_TEXT]


def _keys_of(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        return list(payload)[:30]
    return [type(payload).__name__]


def _session(a: Account, *, force: bool = False) -> _Session:
    with _lock:
        if a.static_token:
            # 长期令牌：不缓存登录态，每次直接用
            return _Session(a.static_token, {}, time.time(), _ttl())
        sess = _sessions.get(a.ref)
        if force or sess is None or sess.expired:
            sess = _login(a)
            _sessions[a.ref] = sess
        return sess


def _credential_placement() -> tuple[str, str]:
    """凭据往哪儿放：(请求头名, 查询参数名)。

    默认按头注入；ITOM 这类平台把登录返回的 gm_auth_token 当**查询参数 uid** 发给每个
    接口（配 MCP_ITOM_UID_PARAM=uid），此时不再多发动作无关的 Authorization——
    带着一个平台不认识的头反而可能被拒。
    """
    uid_param = (cfg("MCP_ITOM_UID_PARAM") or "").strip()
    raw = cfg("MCP_ITOM_TOKEN_HEADER")
    if uid_param:
        return ((raw or "").strip(), uid_param)
    return ((raw if raw is not None else "Authorization").strip(), "")


def _auth_headers(sess: _Session) -> dict[str, str]:
    headers = _platform_headers()
    name, _ = _credential_placement()
    if sess.token and name:
        scheme = cfg("MCP_ITOM_TOKEN_SCHEME") or ""
        headers[name] = f"{scheme} {sess.token}".strip() if scheme else sess.token
    if sess.cookies:
        headers["cookie"] = "; ".join(f"{k}={v}" for k, v in sess.cookies.items())
    return headers


def _auth_params(sess: _Session, params: dict[str, Any] | None) -> dict[str, Any] | None:
    """查询参数模式的凭据注入 + 平台前端的缓存击穿随机数。"""
    _, uid_param = _credential_placement()
    if not uid_param or not sess.token:
        return params or None
    merged: dict[str, Any] = {f"0.{random.randint(0, 10**15 - 1):015d}": ""} if _random_query() else {}
    merged[uid_param] = sess.token
    merged.update(params or {})
    return merged


def _random_query() -> bool:
    """?0.268833697928328&uid=… 里那个裸随机数是平台前端的 cache buster。
    默认不发（后端一般不校验）；平台拒绝无随机数时配 MCP_ITOM_RANDOM_QUERY=1。
    """
    return (cfg("MCP_ITOM_RANDOM_QUERY") or "").strip().lower() in {"1", "true", "yes", "on"}


def read_like(path: str) -> bool:
    """按路径末段判断是否只读语义（findByPage/getList/queryXxx…）。

    平台的分页查询全是 POST，光看方法会把只读查询也拦去审批；这里只认路径，
    不听调用方自称，避免用 read=true 之类的入参绕过闸门。
    """
    extra = (cfg("MCP_ITOM_READ_SEGMENTS") or "").strip()
    words = _READ_SEGMENTS + tuple(w.strip().lower() for w in extra.split(",") if w.strip())
    last = path.rstrip("/").rsplit("/", 1)[-1].lower()
    return any(w in last for w in words)


def _wrap(body: Any) -> Any:
    """平台请求信封：调用方只写业务字段，这里补 {"reqHeader":…,"reqBody":…}。"""
    if body is None:
        return None
    if not isinstance(body, dict):
        return body
    if _ENVELOPE_BODY in body:
        return body
    if (cfg("MCP_ITOM_ENVELOPE") or "1").strip().lower() in {"0", "false", "no", "off"}:
        return body
    return {_ENVELOPE_HEADER: {"operTitle": ""}, _ENVELOPE_BODY: body}


# 平台响应信封：业务数据在 rspBody（登录侧是 reqBody），外层是返回码
_CONTROL_KEYS = {_ENVELOPE_HEADER, _ENVELOPE_BODY, "rspBody", "retCode", "retDesc",
                 "timestamp", "statusCode", "message", "msg"}
_RET_OK = {"0000000", "00000", "0000", "0", "success", "ok", "true"}
# 平台"会话失效"的文案（HTTP 200 + retCode≠0 的形态）——命中才自动重登，避免业务错误被当成掉线
_EXPIRED_WORDS = (
    "会话已失效", "会话失效", "会话过期", " session", "session expired", "invalid session",
    "未登录", "请先登录", "重新登录", "登录失效", "登录过期", "登录超时", "登录已失效",
    "登录信息发生变化", "登录信息已变化", "登录状态异常",
    "token expired", "invalid token", "unauthorized", "认证失效", "身份失效", "not logged in",
)


def _unwrap(payload: Any) -> tuple[Any, dict[str, Any]]:
    """剥信封，返回 (业务体, 控制字段)。

    平台的失败是 **HTTP 200 + retCode≠0000000**，不判 retCode 就会把"会话失效"
    当成功返回给 AI，模型拿着空数据继续往下编——所以业务体为空时也要把控制字段带出来。
    """
    if not isinstance(payload, dict):
        return payload, {}
    for key in ("rspBody", _ENVELOPE_BODY):
        if key in payload and set(payload) <= _CONTROL_KEYS:
            meta = {k: v for k, v in payload.items() if k != key}
            inner = payload[key]
            return (inner if isinstance(inner, (dict, list)) else None), meta
    if set(payload) <= _CONTROL_KEYS - {_ENVELOPE_BODY, "rspBody"}:
        return None, dict(payload)
    return payload, {}


def _find_rows(data: Any) -> tuple[list[dict[str, Any]] | None, Any]:
    """在业务体里定位"行列表"（ITOM 分页是 resultData，也有 list/rows 等写法）。"""
    if isinstance(data, list) and all(isinstance(x, dict) for x in data):
        return data, None
    if isinstance(data, dict):
        for key in ("resultData", "rows", "list", "dataList", "items", "records", "content"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)], key
        # 只有一层嵌套的分页结构也常见：往下找一次
        for v in data.values():
            rows, where = _find_rows(v)
            if rows is not None and len(rows) == len(v if isinstance(v, list) else rows):
                return rows, where
    return None, None


def slim(data: Any, *, fields: list[str] | None = None, max_rows: int = 0) -> Any:
    """按字段白名单投影 + 限行，把"90 列 × 20 行"压成 AI 真正要看的部分。

    平台的工单行里含报障人手机号等个人信息，默认不投影就是把 PII 灌进模型上下文，
    所以业务工具都带字段白名单；通用透传工具可由调用方显式指定 fields。
    """
    rows, where = _find_rows(data)
    if rows is None:
        return data
    picked = fields or None
    out_rows = [{k: r.get(k) for k in picked} for r in rows] if picked else rows
    if max_rows and len(out_rows) > max_rows:
        out_rows = out_rows[:max_rows]
    total = data.get("total") if isinstance(data, dict) else None
    result: dict[str, Any] = {
        "rows": out_rows,
        "returned": len(out_rows),
        "total_rows": len(rows),
    }
    if total is not None:
        result["total"] = total
    if where:
        result["rows_key"] = where
    if picked:
        result["fields"] = picked
    if len(out_rows) < len(rows):
        result["truncated"] = f"仅返回前 {len(out_rows)} 条（共 {len(rows)} 条），" \
                              "请调 max_rows 或翻页"
    return result


def safe_path(path: str) -> str:
    """只允许站内相对路径：挡掉换成任意外部主站的 SSRF 与 ../ 穿越。"""
    p = (path or "").strip()
    if not p.startswith("/"):
        raise ItomError(f"path 必须是以 / 开头的站内相对路径，当前 {path!r}",
                        code=CODE_INVALID_INPUT)
    if "//" in p or ".." in p or p.startswith("//") or "?" in p or "#" in p:
        raise ItomError(
            f"path 不合法（禁止 //、..、查询串与锚点，查询参数请走 params）：{path!r}",
            code=CODE_INVALID_INPUT,
        )
    return check_text(p, limit=500, what="path")


def _auth_expired(meta: dict[str, Any], data: Any) -> bool:
    """平台的"会话失效"是 HTTP 200 + retCode≠0 + 一句中文提示，只能靠文案识别。

    认不出来就当普通业务失败——误判成业务失败最多少一次自动重登，
    反过来把业务错误当会话失效反复重登会更糟。
    """
    text = " ".join(str(v) for v in (meta.get("retDesc"), meta.get("message"),
                                     _error_hint(data) if isinstance(data, dict) else data) if v)
    low = text.lower()
    return any(w in low for w in _EXPIRED_WORDS) or any(w in text for w in _EXPIRED_WORDS)


def request_json(ref: str, method: str, path: str, *, params: dict[str, Any] | None = None,
                 body: Any | None = None, read_channel: bool = False,
                 fields: list[str] | None = None, max_rows: int = 0) -> dict[str, Any]:
    """带鉴权地调平台接口，返回 {ok, status, data}（data 为 JSON 或截断文本）。

    会话复用：进程内按 account_ref 缓存，TTL 内不重复登录（并发下也只登一次）；
    平台提前让会话失效时（401/403，或 HTTP 200 + retCode 的失效文案）**自动重登一次再重试**，
    调用方不需要手动登录，会话由服务端全自动维护。
    read_channel=True（只读工具走这条路）时，非 GET 的方法必须路径像只读接口，
    否则拒绝——平台的分页查询是 POST，但审批闸门不该因此形同虚设。
    """
    a = account(ref)
    verb = (method or "GET").strip().upper()
    if verb not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ItomError(f"不支持的 method: {method!r}", code=CODE_INVALID_INPUT)
    clean = safe_path(path)
    if read_channel and verb != "GET" and not read_like(clean):
        raise ItomError(
            f"itops_query_itom 只放行 GET 或只读形态的路径（末段含 find/get/list/query 等，"
            f"可用 MCP_ITOM_READ_SEGMENTS 追加），{clean!r} 不像只读接口。"
            "确实要写数据请改用 itops_submit_itom（经网关需人工审批）",
            code=CODE_INVALID_INPUT,
        )
    url = _base_url() + clean

    def once(sess: _Session) -> Any:
        return _send(verb, url, params=_auth_params(sess, params), json_body=_wrap(body),
                     headers=_auth_headers(sess))

    relogged = False
    try:
        for attempt in (0, 1):
            with _lock:
                sess = _session(a, force=attempt == 1)
            resp = once(sess)
            if resp.status_code >= 400:
                # 传输层的鉴权失败：还有机会重登；重登过就如实报错
                if resp.status_code in (401, 403) and attempt == 0 and not a.static_token:
                    relogged = True
                    continue
                raise ItomError(
                    f"ITOM {verb} {path} 返回 [{resp.status_code}]：{_snippet(resp.text)}"
                    + ("（重登后仍被拒，请核对账号权限或配 MCP_ITOM_USER_AGENT/_REFERER）"
                       if attempt else ""),
                    code=CODE_HTTP_ERROR,
                )
            data, meta = _unwrap(_parse_body(resp))
            code = str(meta.get("retCode") or "").strip()
            failed = bool(code and code.lower() not in _RET_OK)
            if failed and attempt == 0 and not a.static_token and _auth_expired(meta, data):
                relogged = True
                continue
            if failed:
                expired = _auth_expired(meta, data)
                hint = ""
                if expired and attempt:
                    hint = ("（已自动重登仍被拒：多半是同一账号在浏览器登录把服务端会话顶掉了，"
                            "或平台按登录 IP/上下文绑会话；也可能是服务端没配 MCP_ITOM_UID_PARAM "
                            "导致 uid 与 JSESSIONID 不是同一次登录）")
                elif expired:
                    hint = "（若反复出现请查 it_ops 日志确认是否已在服务端重登）"
                raise ItomError(
                    f"ITOM {path} 业务失败 retCode={code} "
                    f"retDesc={meta.get('retDesc') or _error_hint(data)}" + hint,
                    code=CODE_API_ERROR,
                )
            out: dict[str, Any] = {"ok": True, "status": resp.status_code,
                                   "account_ref": a.ref,
                                   "data": _scrub(slim(data, fields=fields, max_rows=max_rows))}
            if meta.get("retCode") is not None:
                out["retCode"] = meta["retCode"]
            if relogged:
                out["relogged"] = True  # 透明：本次调用中途重登过，便于排查会话时长设置
            return out
    except ItomError:
        raise
    except httpx2.HTTPError as e:
        raise ItomError(
            f"调用 ITOM 失败（{verb} {url}）：{type(e).__name__}: {e}。"
            "请确认 MCP_ITOM_URL 与网络可达，或稍后重试",
            code=CODE_UNREACHABLE,
        ) from e
    raise ItomError(f"ITOM {verb} {path} 未预期地走完重试循环")  # 循环必然 return/raise，兜底


def _error_hint(data: Any) -> str:
    """平台把失败原因写在若干候选键里，取出来拼成一行。"""
    if not isinstance(data, dict):
        return ""
    for key in ("message", "msg", "errMsg", "error", "errorMsg", "retMsg"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:300]
    body = data.get("reqBody")
    return _error_hint(body) if isinstance(body, dict) else ""


INCIDENT_PATH = "/event-manage/findByPage"
# 事件单默认投影列：够定位与跟进而不含手机号等个人信息（fields 可显式覆盖）
INCIDENT_FIELDS = (
    "eventNo", "eventState", "eventLevel", "eventNature", "problemDesc",
    "systemTypeName", "systemSubclassName", "repairDeptName", "repairPostName",
    "groupName", "dealStaffName", "createdByName", "createdDt",
    "responseTime", "solveTime", "processingProgress",
)

# 平台码值字典（/base-dict-data/findByParams：typeCode 取父项 → parentSid 取子项）。
# 运行时优先查平台，下面这份是 2026-09-28 取证快照，只作平台不可达时的兜底。
CODE_TABLES = {
    "EVENT_LEVEL": {"1": "低", "2": "中", "3": "高", "4": "紧急"},
    "EVENT_NATURE": {"1": "故障", "2": "服务请求", "3": "监控告警", "4": "信息安全"},
    "EVENT_STATUS": {"0": "全部", "1": "已分配", "2": "处理中", "3": "已解决",
                     "4": "记录解决方案", "5": "回访", "6": "遗留", "7": "关闭",
                     "8": "置废", "9": "用户新建", "10": "用户提报", "11": "监控新建"},
    "EVENT_CATEGORY": {"1": "软件", "2": "硬件"},
    "SYS_USER_TYPE": {"1": "一线人员", "2": "二线人员", "3": "系统负责人"},
    "SOLVE_MODE": {"1": "电话指导", "2": "远程解决", "3": "现场解决"},
    "SOLVE_TYPE": {"A": "技术类", "B": "一二级类", "C": "业务类", "D": "系统集成类",
                   "E": "其他类", "F": "安装类", "G": "调试类", "H": "配置类",
                   "I": "天车类", "J": "系统集成类", "K": "发布类", "L": "硬件损坏类",
                   "M": "信息安全类", "N": "信息安全类"},
    "CHANGE_ORDER_REASON": {"1": "用户提报错误", "2": "遗留重新分配", "3": "服务台分配错误",
                            "4": "其他系统问题", "5": "内部转单"},
    "NO_ANSWER": {"1": "无人接听", "2": "拒接", "3": "停机", "4": "换号未在系统内维护"},
    "QUESTION_STATUS": {"0": "全部", "1": "待评估", "2": "待处理", "3": "处理中", "4": "挂起",
                        "5": "已关闭", "6": "置废", "7": "已解决", "8": "待确认",
                        "9": "确认未通过", "20": "观察中"},
    "IS_SATISFIED": {"0": "是", "1": "否"},
    "IS_SOLVED": {"0": "是", "1": "否"},
}
# 行内码值 → 中文含义的映射（列表返回时补齐，省得 AI 猜 "3" 是什么状态）
_DECODE_FIELDS = {"eventState": "EVENT_STATUS", "eventLevel": "EVENT_LEVEL",
                  "eventNature": "EVENT_NATURE", "eventCategory": "EVENT_CATEGORY",
                  "dealStaffRole": "SYS_USER_TYPE", "solveMode": "SOLVE_MODE"}
_dict_cache: dict[str, tuple[dict[str, str], float]] = {}
_DICT_TTL = 3600.0


def code_table(name: str, ref: str | None = None) -> dict[str, str]:
    """取码表：平台字典优先，取不到用内置快照。

    平台的字典是两级（父项 typeCode → parentSid → 子项），只读接口，
    结果按 name 缓存 1 小时——码值表变更不频繁，没必要每次建单都查两趟。
    """
    cached = _dict_cache.get(name)
    if cached and time.time() - cached[1] < _DICT_TTL:
        return cached[0]
    fallback = CODE_TABLES.get(name, {})
    if not ref:
        return fallback
    try:
        parent = request_json(ref, "POST", "/base-dict-data/findByParams",
                              body={"typeCode": name, "pageSize": "5"}, read_channel=True)
        rows = (parent["data"] or {}).get("rows") or []
        sid = rows[0].get("typeSid") if rows else None
        if not sid:
            return fallback
        kids = request_json(ref, "POST", "/base-dict-data/findByParams",
                            body={"parentSid": sid, "pageSize": "80"}, read_channel=True,
                            fields=["typeCode", "typeName"], max_rows=80)
        table = {str(k["typeCode"]): k["typeName"]
                 for k in (kids["data"] or {}).get("rows") or [] if k.get("typeCode")}
        if not table:
            return fallback
    except ItomError:
        return fallback
    _dict_cache[name] = (table, time.time())
    return table


def decode_rows(rows: list[dict[str, Any]], ref: str | None = None) -> list[dict[str, Any]]:
    """给行补 `<字段>Name` 中文含义（未知码值原样保留，不编造）。

    只查行里真正出现过的码表——不然一次列表要白打六个字典接口，
    平台慢的时候这点开销会被 AI 的超时重试放大。
    """
    needed = {field for field in _DECODE_FIELDS
              if any(row.get(field) not in (None, "") for row in rows)}
    tables = {field: code_table(_DECODE_FIELDS[field], ref) for field in needed}
    for row in rows:
        for field, table in tables.items():
            label = table.get(str(row.get(field)))
            if label:
                row[f"{field}Name"] = label
    return rows


def incidents(ref: str, *, days: int = 7, system_type: str | None = None,
              event_no: str | None = None, page_num: int = 1, page_size: int = 20,
              fields: list[str] | None = None) -> dict[str, Any]:
    """事件单列表（分页 + 时间窗 + 系统类型/单号过滤），默认只回投影列。

    平台的时间过滤是 createdDtGt/Lt 一对字符串，窗口不给就查不到东西，
    所以这里按 days 生成，避免 AI 每次自己拼日期格式。

    实测边界（别指望更多）：`eventState` 传任何值都返回 0 行（平台侧未按该字段过滤），
    `problemDesc` 只做整句精确匹配——所以状态/关键字过滤都没做进工具，
    要按状态统计请在返回结果里自行分组，或抓平台搜索框的真实接口再接。
    """
    now = datetime.now()
    body: dict[str, Any] = {
        "systemSubclassSid": None,
        "createdDtGt": (now - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S"),
        "createdDtLt": (now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
        "groupCode": None,
        "pageNum": str(int(page_num)),
        "pageSize": str(max(1, min(int(page_size), 100))),
    }
    if system_type:
        body["systemType"] = str(system_type)
    if event_no:
        body["eventNo"] = str(event_no)
    picked = list(fields) if fields else list(INCIDENT_FIELDS)
    out = request_json(ref, "POST", INCIDENT_PATH, body=body,
                       read_channel=True, fields=picked, max_rows=int(page_size))
    rows = (out.get("data") or {}).get("rows")
    if isinstance(rows, list):
        out["data"]["rows"] = decode_rows(rows, ref)
    return out


# ---------------------------------------------------------------------------
# 引导式建单：下拉选项发现 + 名称→代码解析 + 表单装配
# ---------------------------------------------------------------------------

# 敏感列：平台有些档案接口把身份证号、用户 token 一起返回（如 auth-user/findByParamsForET
# 的 75 列），这些绝不能经透传工具进 AI 上下文与审计。
SCRUB_COLUMNS = frozenset({
    "idno", "idcard", "id_no", "cardno", "token", "accesstoken", "password", "passwd",
    "secret", "auth_token", "gm_auth_token", "sessionid", "jsessionid",
})

# 各选项类别：接口、请求体里父级键名、投影列（白名单，不给宽行）
OPTION_KINDS: dict[str, tuple[str, str | None, tuple[str, ...], str]] = {
    "dept": ("/auth/auth-org-info/findDeOpsOrgInfoByConfig", None,
             ("orgCode", "orgDesc", "orgSid", "parentOrgCode", "companyCode"), "orgDesc"),
    "group": ("/auth/auth-org-info/findOrgInfoByOrgCode", "orgCode",
              ("orgCode", "orgDesc", "orgSid", "parentOrgCode"), "orgDesc"),
    "system": ("/bs-system-class/findBaseDictDataMapOrgCode", "groupCode",
               ("sysSid", "sysCode", "sysName", "sysLevel", "groupCode", "orgCode"), "sysName"),
    "subclass": ("/bs-system-class/findBaseDictDataMapOrgCode", "parentSid",
                 ("sysSid", "sysCode", "sysName", "sysLevel", "parentSid"), "sysName"),
    "menu": ("/bs-system-class/findBaseDictDataMapOrgCode", "parentSid",
             ("sysSid", "sysCode", "sysName", "sysLevel", "parentSid"), "sysName"),
    # 报修部门与「部门分类」不是一个层级：按名称能查到多个同名不同级的组织，
    # 必须靠上级码/companyCode 消歧，解析不出来就在预览里标出来让人确认
    "repairDept": ("/auth/auth-org-info/findByParams", None,
                   ("orgCode", "orgDesc", "orgSid", "parentOrgCode", "companyCode"), "orgDesc"),
    "custom": ("/bs-custom-info/findByPage", None,
               ("sid", "customName", "mobilePhone", "departentName", "operationName", "postName"),
               "customName"),
    "staff": ("/bs-system-class-staff/findByParamsForEvent", "sysSid",
              ("userId", "userName", "mobilePhone", "sysUserType", "isDefault", "sysCode", "sysName"),
              "userName"),
}
# 类别 → 用户输入里的关键字段名（custom 按姓名查、staff 按姓名/工号查）
_KEYWORD_FIELD = {"custom": "customName", "staff": "userName", "dept": "orgDesc",
                  "repairDept": "orgDesc",
                  "group": "orgDesc", "system": "sysName", "subclass": "sysName", "menu": "sysName"}
# 码值类别（走字典，不打接口）
DICT_KINDS = {"level": "EVENT_LEVEL", "nature": "EVENT_NATURE", "state": "EVENT_STATUS",
              "role": "SYS_USER_TYPE", "category": "EVENT_CATEGORY", "solveMode": "SOLVE_MODE"}

# 建单表单字段说明（给 AI 的说明书：必填、来源、示例）
INCIDENT_FORM = (
    {"field": "dept", "label": "部门分类", "required": True, "kind": "dept",
     "note": "名称或 orgCode；对应 deptCode/deptSid/deptName"},
    {"field": "group", "label": "小组", "required": True, "kind": "group",
     "note": "需先定 dept（parent=部门 orgCode）；对应 groupCode/groupSid/groupName"},
    {"field": "system", "label": "系统分类", "required": True, "kind": "system",
     "note": "需先定 group（parent=groupCode）；对应 systemType/systemTypeSid/systemTypeName"},
    {"field": "subclass", "label": "系统子类", "required": True, "kind": "subclass",
     "note": "parent=系统的 sysSid（必须是一级系统的 sid，传子类 sid 查不到）"},
    {"field": "menu", "label": "报修菜单", "required": True, "kind": "menu",
     "note": "parent=子类的 sysSid"},
    {"field": "reporter", "label": "报修人", "required": True, "kind": "custom",
     "note": "按姓名查客户档案，自动回填电话/部门/作业区/岗位；平台存在同名，命中多条必须回问"},
    {"field": "level", "label": "事件等级", "required": True, "kind": "dict:level",
     "note": "1低/2中/3高/4紧急，可传中文"},
    {"field": "nature", "label": "事件性质", "required": True, "kind": "dict:nature",
     "note": "1故障/2服务请求/3监控告警/4信息安全，可传中文"},
    {"field": "role", "label": "人员角色", "required": True, "kind": "dict:role",
     "note": "1一线人员/2二线人员/3系统负责人；同时是处理人查询的 sysUserType"},
    {"field": "handler", "label": "处理人", "required": True, "kind": "staff",
     "note": "parent=系统 sysSid + 角色；自动回填 dealStaff/dealStaffName/dealStaffPhone"},
    {"field": "description", "label": "问题描述", "required": True, "kind": "text",
     "note": "自由文本，≤500 字"},
)


def _scrub(payload: Any) -> Any:
    """递归剔除敏感列——透传工具把档案接口原样返回时兜底。"""
    if isinstance(payload, dict):
        return {k: _scrub(v) for k, v in payload.items()
                if str(k).lower() not in SCRUB_COLUMNS}
    if isinstance(payload, list):
        return [_scrub(v) for v in payload]
    return payload


def options(ref: str, kind: str, *, parent: str | int | None = None,
            keyword: str | None = None, limit: int = 50,
            role: str | None = None) -> list[dict[str, Any]]:
    """列出一个下拉的候选项（精简投影），供 AI 引导用户选择。

    kind=form 返回建单表单字段说明书（承接原独立表单工具，能力不丢）。
    parent 是上一级选中的代码：group 传部门 orgCode、system 传 groupCode、
    subclass/menu 传上级 sysSid、staff 传系统 sysSid（必须一级系统）。
    role 仅 staff 用（1 一线/2 二线/3 系统负责人）——平台按 sysSid+sysUserType 两个键
    才查得到人，缺一个就返回空列表。
    """
    kind = (kind or "").strip()
    if kind == "form":
        return list(INCIDENT_FORM)
    if kind in DICT_KINDS:
        table = code_table(DICT_KINDS[kind], ref)
        return [{"code": c, "name": n} for c, n in sorted(table.items(), key=lambda x: x[0])]
    if kind not in OPTION_KINDS:
        raise ItomError(
            f"未知的选项类别 {kind!r}；可用：form（建单表单说明书）、"
            f"{', '.join(list(OPTION_KINDS) + [f'dict:{v}' for v in DICT_KINDS])}",
            code=CODE_INVALID_INPUT,
        )
    path, parent_key, cols, name_key = OPTION_KINDS[kind]
    body: dict[str, Any] = {**({"pageSize": str(max(1, min(int(limit), 200)))})}
    if parent_key:
        if parent in (None, ""):
            raise ItomError(f"kind={kind} 必须先确定上一级（body 需要 {parent_key}）；"
                            "先调本工具拿上级候选项再传 parent", code=CODE_INVALID_INPUT)
        body[parent_key] = parent if not parent_key.endswith("Sid") else int(parent)
    if kind == "staff":
        if not role:
            raise ItomError("kind=staff 必须同时给 role（1 一线/2 二线/3 系统负责人），"
                            "否则平台返回 0 个处理人", code=CODE_INVALID_INPUT)
        body["sysUserType"] = str(role)
    if keyword:
        body[_KEYWORD_FIELD.get(kind, "keyword")] = keyword
    out = request_json(ref, "POST", path, body=body, read_channel=True, fields=list(cols))
    rows = (out.get("data") or {}).get("rows") or []
    # 只按关键字过滤接口没实现模糊查询的类别（custom 平台是精确匹配）
    if keyword and kind not in {"custom"}:
        rows = [r for r in rows if keyword.lower() in str(r.get(name_key, "")).lower()]
    return rows[: int(limit)]


def resolve(ref: str, kind: str, value: str, *, parent: str | int | None = None,
            hint: str | None = None, role: str | None = None) -> dict[str, Any]:
    """把用户说的名称解析成唯一一条候选；**歧义一律报错回问，绝不挑第一个**。

    hint 用于同名消歧（如报修人重名时给部门名）——hint 能唯一命中才继续。
    """
    if not value or not str(value).strip():
        raise ItomError(f"{kind} 不能为空", code=CODE_INVALID_INPUT)
    value = str(value).strip()
    if kind in DICT_KINDS:
        table = code_table(DICT_KINDS[kind], ref)
        if value in table:
            return {"code": value, "name": table[value]}
        hits = [(c, n) for c, n in table.items() if n == value or value in n]
        if len(hits) == 1:
            return {"code": hits[0][0], "name": hits[0][1]}
        if len(hits) > 1:
            raise ItomError(f"{kind}={value!r} 匹配到多个码值：{hits}，请让用户确认",
                            code=CODE_AMBIGUOUS)
        raise ItomError(f"{kind}={value!r} 不在码表里；可选：{table}",
                        code=CODE_NOT_FOUND)
    _, _, cols, name_key = OPTION_KINDS[kind]
    # 带关键字查：客户档案这类大表翻页取前 200 条是碰不到目标人的
    rows = options(ref, kind, parent=parent, keyword=value, limit=200, role=role)
    exact = [r for r in rows if str(r.get(name_key, "")).strip() == value]
    if not exact:  # 允许直接传代码
        code_key = cols[0]
        exact = [r for r in rows if str(r.get(code_key, "")) == value]
    if hint:
        narrowed = [r for r in exact if any(hint in str(v) for v in r.values())]
        if narrowed:
            exact = narrowed
    if not exact:
        sample = sorted({str(r.get(name_key)) for r in rows})[:15]
        raise ItomError(
            f"没找到 {kind}={value!r}"
            + (f"（上级 parent={parent}）" if parent else "")
            + f"；该层级可选前 15 项：{sample}。请让用户重新选择，不要猜。",
            code=CODE_NOT_FOUND,
        )
    if len(exact) > 1:
        # 逐行列出区分字段（截断 JSON 会把值切成半截，AI 就没法回问用户）
        lines = [" | ".join(f"{k}={r.get(k)}" for k in cols if r.get(k) not in (None, ""))
                 for r in exact[:8]]
        raise ItomError(
            f"{kind}={value!r} 在平台有 {len(exact)} 条同名记录，必须让用户确认是哪一条：\n"
            + "\n".join(lines)
            + ("\n（把区分信息放进 hint 参数再调一次）" if not hint else ""),
            code=CODE_AMBIGUOUS,
        )
    return exact[0]


def build_incident(fields: dict[str, Any]) -> dict[str, Any]:
    """把已解析好的候选行拼成 saveData 的 reqBody（与平台前端提交形态一致）。"""
    dept, group = fields["dept"], fields["group"]
    system, subclass, menu = fields["system"], fields["subclass"], fields["menu"]
    reporter, handler = fields["reporter"], fields["handler"]
    role = fields["role"]
    return {
        "deptCode": dept["orgCode"], "deptSid": dept["orgSid"], "deptName": dept["orgDesc"],
        "groupCode": group["orgCode"], "groupSid": group["orgSid"], "groupName": group["orgDesc"],
        "systemType": system["sysCode"], "systemTypeSid": system["sysSid"],
        "systemTypeName": system["sysName"],
        "systemSubclass": subclass["sysCode"], "systemSubclassSid": subclass["sysSid"],
        "systemSubclassName": subclass["sysName"],
        "repairMenu": menu["sysCode"], "repairMenuSid": menu["sysSid"], "repairMenuName": menu["sysName"],
        "repairStaffName": reporter.get("customName"),
        "repairStaffPhone": fields.get("reporter_phone") or reporter.get("mobilePhone"),
        "repairDeptCode": fields.get("repair_dept_code"),
        "repairDeptName": reporter.get("departentName"),
        "repairOperationArea": reporter.get("operationName"),
        "repairPostName": reporter.get("postName"),
        "repairDeptRemark": fields.get("remark") or "",
        "eventLevel": fields["level"]["code"], "eventNature": fields["nature"]["code"],
        "dealStaffRole": role["code"],
        "dealStaff": handler["userId"], "dealStaffName": handler["userName"],
        "dealStaffPhone": handler.get("mobilePhone"),
        "problemDesc": fields["description"],
        "inChargeStaff": None, "inChargeStaffName": None, "inChargePhone": "",
        "systemLevel": "", "operTitle": "",
    }


def prepare_incident(ref: str, *, dept: str, group: str, system: str, subclass: str, menu: str,
                     reporter: str, level: str, nature: str, role: str, handler: str,
                     description: str, repair_dept_code: str | None = None,
                     reporter_phone: str | None = None, reporter_hint: str | None = None,
                     handler_hint: str | None = None) -> dict[str, Any]:
    """逐级解析建单字段（父级必须先解析，天然形成引导顺序）。"""
    d = resolve(ref, "dept", dept)
    g = resolve(ref, "group", group, parent=d["orgCode"])
    s = resolve(ref, "system", system, parent=g["orgCode"])
    sc = resolve(ref, "subclass", subclass, parent=s["sysSid"])
    m = resolve(ref, "menu", menu, parent=sc["sysSid"])
    r = resolve(ref, "custom", reporter, hint=reporter_hint)
    lv = resolve(ref, "level", level)
    na = resolve(ref, "nature", nature)
    ro = resolve(ref, "role", role)
    h = resolve(ref, "staff", handler, parent=s["sysSid"], role=ro["code"], hint=handler_hint)
    if not (r.get("mobilePhone") or reporter_phone):
        raise ItomError(
            f"报修人 {reporter!r} 的客户档案里没有电话，而平台建单必填："
            "请让用户提供报修电话后重试（reporter_phone 参数）",
            code=CODE_INVALID_INPUT,
        )
    warnings: list[str] = []
    rd_code = repair_dept_code
    if not rd_code and r.get("departentName"):
        # 报修部门代码要能解析出来；同名多条时不猜，标在 warnings 里回问用户
        try:
            rd_code = resolve(ref, "repairDept", str(r["departentName"]))["orgCode"]
        except ItomError as e:
            warnings.append(f"报修部门代码未解析：{str(e)[:420]}；"
                            "请让用户从上面候选里确认后传 repair_dept_code")

    fields: dict[str, Any] = {
        "dept": d, "group": g, "system": s, "subclass": sc, "menu": m, "reporter": r,
        "level": lv, "nature": na, "role": ro, "handler": h,
        "description": check_text(description, limit=500, what="description"),
        "reporter_phone": reporter_phone, "repair_dept_code": rd_code,
    }
    return {"resolved": {"dept": d, "group": g, "system": s, "subclass": sc, "menu": m,
                         "reporter": r, "handler": h, "level": lv, "nature": na, "role": ro},
            "payload": build_incident(fields), "warnings": warnings}


def submit_incident(ref: str, payload: dict[str, Any]) -> dict[str, Any]:
    """真实提交建单（调用方必须已经让用户确认过 payload）。"""
    return request_json(ref, "POST", "/event-manage/saveData", body=payload)


def selfcheck() -> dict[str, Any]:
    """ITOM 对接自检：只回配置"有没有/是什么形态"，绝不回值。

    0200000「登录信息发生变化」这类问题九成是配置形态不对（缺 UID_PARAM、
    配置文件不在以为的位置），以前只能登服务器 grep，现在一条工具调用说清楚。
    """
    from mcp_shared.config import config_path

    path = config_path()
    head, uid_param = _credential_placement()
    sessions = {}
    with _lock:
        for ref, sess in _sessions.items():
            sessions[ref] = {"has_token": bool(sess.token), "cookies": sorted(sess.cookies),
                             "age_s": round(time.time() - sess.obtained_at, 1),
                             "expired": sess.expired}
    return {
        "config_file": str(path) if path else None,
        "url_set": bool(cfg("MCP_ITOM_URL")),
        "accounts": configured_refs(),
        "uid_param": uid_param or None,
        "token_header": head or None,
        "envelope": (cfg("MCP_ITOM_ENVELOPE") or "1").strip().lower() not in {"0", "false", "no", "off"},
        "token_ttl_s": _ttl(),
        "timeout_s": _timeout(),
        "sessions": sessions,
        "hint": None if uid_param else
                "未配 MCP_ITOM_UID_PARAM：ITOM 的业务接口要求把登录返回的 gm_auth_token "
                "作为查询参数 uid 发送，缺了它平台会回 retCode=0200000「登录信息发生变化」",
    }


def reset(ref: str | None = None) -> list[str]:
    """清掉进程内会话（口令改了、或想把平台侧 session 打掉时调用）。"""
    with _lock:
        if ref:
            a = account(ref)
            existed = _sessions.pop(a.ref, None) is not None
            _last_source.pop(a.ref, None)
            return [a.ref] if existed else []
        refs = sorted(_sessions)
        _sessions.clear()
        _last_source.clear()
        return refs
