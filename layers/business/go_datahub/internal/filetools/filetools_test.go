package filetools

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeFile(t *testing.T, dir, name, content string) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestHashFile(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "a.txt", "hello")

	hexSum, size, err := HashFile(p, "")
	if err != nil {
		t.Fatal(err)
	}
	// sha256("hello") 的标准值
	const want = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
	if hexSum != want {
		t.Fatalf("默认 sha256 结果错误: %s", hexSum)
	}
	if size != 5 {
		t.Fatalf("size=%d want 5", size)
	}
	if _, _, err := HashFile(p, "md5"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := HashFile(p, "sm3"); err == nil {
		t.Fatal("非法算法应报错")
	}
	if _, _, err := HashFile(filepath.Join(dir, "missing"), "sha256"); err == nil {
		t.Fatal("文件不存在应报错")
	}
}

func TestFileStats(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "s.txt", "line1\nline2\nline3")

	st, err := FileStats(p, 2)
	if err != nil {
		t.Fatal(err)
	}
	if st.Lines != 3 || st.Encoding != "ascii" || st.SizeBytes != 17 {
		t.Fatalf("stats=%+v", st)
	}
	if len(st.Preview) != 2 || st.Preview[0] != "line1" || !st.Truncated {
		t.Fatalf("preview=%v truncated=%v", st.Preview, st.Truncated)
	}

	// UTF-8 BOM + 中文
	bom := writeFile(t, dir, "bom.txt", "\xEF\xBB\xBF名字\n值")
	st, err = FileStats(bom, 0)
	if err != nil {
		t.Fatal(err)
	}
	if st.Encoding != "utf-8-bom" || !st.HasBOM {
		t.Fatalf("bom stats=%+v", st)
	}

	// GBK（非法 UTF-8 高位字节）
	gbk := writeFile(t, dir, "gbk.txt", "\xc4\xfa\xba\xc3\n")
	st, err = FileStats(gbk, 0)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(st.Encoding, "non-utf8") {
		t.Fatalf("gbk encoding=%s", st.Encoding)
	}

	// 二进制
	bin := writeFile(t, dir, "bin.dat", "\x00\x01\x02")
	st, err = FileStats(bin, 0)
	if err != nil {
		t.Fatal(err)
	}
	if st.Encoding != "binary" || len(st.Preview) != 0 {
		t.Fatalf("binary stats=%+v", st)
	}

	if _, err := FileStats(dir, 0); err == nil {
		t.Fatal("目录应报错")
	}
}

func TestConvertCsvToJsonlAndBack(t *testing.T) {
	dir := t.TempDir()
	csvPath := writeFile(t, dir, "in.csv", "id,name\n1,alice\n2,\"bo\"\"b\"\n")

	res, err := ConvertFile(csvPath, filepath.Join(dir, "out.jsonl"), "", 0)
	if err != nil {
		t.Fatal(err)
	}
	if res.To != "jsonl" || res.RowsConverted != 2 || res.Truncated {
		t.Fatalf("csv→jsonl res=%+v", res)
	}
	raw, _ := os.ReadFile(filepath.Join(dir, "out.jsonl"))
	lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
	var rec map[string]string
	if err = json.Unmarshal([]byte(lines[1]), &rec); err != nil {
		t.Fatal(err)
	}
	if rec["name"] != `bo"b` {
		t.Fatalf("引号转义丢失: %v", rec)
	}

	// 回流 CSV
	res, err = ConvertFile(filepath.Join(dir, "out.jsonl"), filepath.Join(dir, "back.csv"), "", 0)
	if err != nil {
		t.Fatal(err)
	}
	if res.To != "csv" || res.RowsConverted != 2 {
		t.Fatalf("jsonl→csv res=%+v", res)
	}
	raw, _ = os.ReadFile(filepath.Join(dir, "back.csv"))
	if !strings.HasPrefix(string(raw), "id,name\n") {
		t.Fatalf("表头列序错误: %s", raw)
	}
	// .tmp 中间文件不应残留
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".tmp") {
			t.Fatalf("残留临时文件: %s", e.Name())
		}
	}
}

func TestConvertMaxRowsAndErrors(t *testing.T) {
	dir := t.TempDir()
	csvPath := writeFile(t, dir, "m.csv", "id\n1\n2\n3\n")
	res, err := ConvertFile(csvPath, filepath.Join(dir, "m.jsonl"), "", 2)
	if err != nil {
		t.Fatal(err)
	}
	if res.RowsConverted != 2 || !res.Truncated {
		t.Fatalf("max_rows 截断失败: %+v", res)
	}

	if _, err = ConvertFile(writeFile(t, dir, "x.txt", "a"), filepath.Join(dir, "y"), "", 0); err == nil {
		t.Fatal("不支持的扩展名应报错")
	}
	if _, err = ConvertFile(csvPath, filepath.Join(dir, "z.jsonl"), "csv", 0); err == nil {
		t.Fatal("to 显式非法组合应报错")
	}
	// 非法 JSON 行：报错且不残留 .tmp
	bad := writeFile(t, dir, "bad.jsonl", "{\"a\":1}\nnot json\n")
	if _, err = ConvertFile(bad, filepath.Join(dir, "bad.csv"), "", 0); err == nil {
		t.Fatal("非法 JSONL 应报错")
	}
	if _, err = os.Stat(filepath.Join(dir, "bad.csv.tmp")); !os.IsNotExist(err) {
		t.Fatal("失败后应清理 .tmp")
	}
}
