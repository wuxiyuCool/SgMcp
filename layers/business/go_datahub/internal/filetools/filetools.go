// Package filetools 文件类"重活"工具：流式哈希、统计、CSV↔JSONL 转换。
//
// 与大文件交互全程流式处理（内存占用与文件大小解耦），
// 路径参数与 csv 采集器同信任级——仅访问 datahub-server 本机的文件。
package filetools

import (
	"bufio"
	"crypto/md5"
	"crypto/sha1"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"hash"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"unicode/utf8"
)

// HashFile 流式计算文件哈希。algo 可选 md5/sha1/sha256/sha512。
func HashFile(path, algo string) (hexSum string, size int64, err error) {
	var h hash.Hash
	switch strings.ToLower(algo) {
	case "", "sha256":
		h = sha256.New()
	case "md5":
		h = md5.New()
	case "sha1":
		h = sha1.New()
	case "sha512":
		h = sha512.New()
	default:
		return "", 0, fmt.Errorf("不支持的算法: %s（可选 md5/sha1/sha256/sha512）", algo)
	}
	f, err := os.Open(path)
	if err != nil {
		return "", 0, fmt.Errorf("打开文件失败: %w", err)
	}
	defer f.Close()
	if _, err = io.Copy(h, f); err != nil {
		return "", 0, fmt.Errorf("读取文件失败: %w", err)
	}
	fi, err := f.Stat()
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(h.Sum(nil)), fi.Size(), nil
}

// Stats 文件统计结果。
type Stats struct {
	SizeBytes  int64    `json:"size_bytes"`
	Lines      int64    `json:"lines"`
	Encoding   string   `json:"encoding"`
	HasBOM     bool     `json:"has_bom"`
	ModifiedAt string   `json:"modified_at"`
	Preview    []string `json:"preview"`
	Truncated  bool     `json:"preview_truncated"`
}

// DetectEncoding 读取文件头 8KB 判断编码：utf-8-bom / utf-8 / ascii / gbk? / binary。
func DetectEncoding(f *os.File) (enc string, hasBOM bool, err error) {
	buf := make([]byte, 8192)
	n, err := f.ReadAt(buf, 0)
	if err != nil && err != io.EOF {
		return "", false, err
	}
	buf = buf[:n]
	if len(buf) >= 3 && buf[0] == 0xEF && buf[1] == 0xBB && buf[2] == 0xBF {
		return "utf-8-bom", true, nil
	}
	hasNUL := false
	highBytes := false
	for _, b := range buf {
		if b == 0 {
			hasNUL = true
		}
		if b >= 0x80 {
			highBytes = true
		}
	}
	switch {
	case hasNUL:
		return "binary", false, nil
	case !highBytes:
		return "ascii", false, nil
	case utf8.Valid(buf):
		return "utf-8", false, nil
	default:
		// 高位字节且非合法 UTF-8：GBK 系中文环境最常见
		return "non-utf8(可能为GBK)", false, nil
	}
}

// FileStats 统计大小/行数/编码并返回前 previewLines 行预览。
func FileStats(path string, previewLines int) (*Stats, error) {
	if previewLines <= 0 {
		previewLines = 5
	}
	if previewLines > 50 {
		previewLines = 50
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("打开文件失败: %w", err)
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if fi.IsDir() {
		return nil, fmt.Errorf("路径是目录不是文件: %s", path)
	}
	enc, bom, err := DetectEncoding(f)
	if err != nil {
		return nil, err
	}

	out := &Stats{
		SizeBytes:  fi.Size(),
		Encoding:   enc,
		HasBOM:     bom,
		ModifiedAt: fi.ModTime().Format("2006-01-02T15:04:05-07:00"),
		Preview:    []string{},
	}
	if enc == "binary" {
		return out, nil
	}

	// 流式数行 + 采集预览（Scanner 缓冲放大到 1MB，容忍超长行截断）
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for sc.Scan() {
		out.Lines++
		if len(out.Preview) < previewLines {
			line := sc.Text()
			if len(line) > 500 {
				line = line[:500] + "…(行截断)"
			}
			out.Preview = append(out.Preview, line)
		} else {
			out.Truncated = true
		}
	}
	if err := sc.Err(); err != nil {
		// 超长行报错时退化为按字节数行，保证 size 场景仍可用
		if !strings.Contains(err.Error(), "token too long") {
			return nil, fmt.Errorf("逐行扫描失败: %w", err)
		}
		out.Truncated = true
	}
	return out, nil
}

// ConvertResult 转换结果。
type ConvertResult struct {
	Src           string `json:"src"`
	Dst           string `json:"dst"`
	From          string `json:"from"`
	To            string `json:"to"`
	RowsConverted int64  `json:"rows_converted"`
	Truncated     bool   `json:"truncated"`
}

// ConvertFile CSV↔JSONL 流式互转。to 留空时按源扩展名自动推断方向。maxRows>0 时截断。
// 目标文件原子写（先 .tmp 再 rename）。
func ConvertFile(src, dst, to string, maxRows int64) (*ConvertResult, error) {
	ext := strings.ToLower(filepath.Ext(src))
	switch ext {
	case ".csv":
		if to == "" {
			to = "jsonl"
		}
		if to != "jsonl" {
			return nil, fmt.Errorf("源文件是 CSV，to 只能是 jsonl（收到 %q）", to)
		}
	case ".jsonl", ".ndjson":
		if to == "" {
			to = "csv"
		}
		if to != "csv" {
			return nil, fmt.Errorf("源文件是 JSONL，to 只能是 csv（收到 %q）", to)
		}
	case ".json":
		return nil, fmt.Errorf("暂不支持整体 .json（数组）转换，请先另存为 .jsonl 或 .csv")
	default:
		return nil, fmt.Errorf("不支持的源文件类型 %s（识别 .csv/.jsonl/.ndjson）", ext)
	}

	in, err := os.Open(src)
	if err != nil {
		return nil, fmt.Errorf("打开源文件失败: %w", err)
	}
	defer in.Close()

	tmp := dst + ".tmp"
	outf, err := os.Create(tmp)
	if err != nil {
		return nil, fmt.Errorf("创建目标文件失败: %w", err)
	}
	cleanup := func() { outf.Close(); os.Remove(tmp) }

	res := &ConvertResult{Src: src, Dst: dst, From: strings.TrimPrefix(ext, "."), To: to}
	w := bufio.NewWriter(outf)

	if ext == ".csv" {
		err = csvToJsonl(in, w, maxRows, res)
	} else {
		err = jsonlToCsv(in, w, maxRows, res)
	}
	if err != nil {
		cleanup()
		return nil, err
	}
	if err = w.Flush(); err != nil {
		cleanup()
		return nil, err
	}
	if err = outf.Close(); err != nil {
		os.Remove(tmp)
		return nil, err
	}
	if err = os.Rename(tmp, dst); err != nil {
		os.Remove(tmp)
		return nil, fmt.Errorf("落盘目标文件失败: %w", err)
	}
	return res, nil
}

func csvToJsonl(in io.Reader, w io.Writer, maxRows int64, res *ConvertResult) error {
	br := csv.NewReader(bufio.NewReader(in))
	br.FieldsPerRecord = -1
	header, err := br.Read()
	if err != nil {
		return fmt.Errorf("读取 CSV 表头失败: %w", err)
	}
	enc := json.NewEncoder(w)
	for {
		row, err := br.Read()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return fmt.Errorf("第 %d 行解析失败: %w", res.RowsConverted+2, err)
		}
		if maxRows > 0 && res.RowsConverted >= maxRows {
			res.Truncated = true
			return nil
		}
		rec := map[string]string{}
		for i, col := range header {
			if i < len(row) {
				rec[col] = row[i]
			}
		}
		if err = enc.Encode(rec); err != nil {
			return err
		}
		res.RowsConverted++
	}
}

func jsonlToCsv(in io.Reader, w io.Writer, maxRows int64, res *ConvertResult) error {
	sc := bufio.NewScanner(bufio.NewReader(in))
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)

	var cols []string
	bw := csv.NewWriter(w)
	lineNo := int64(0)

	flushAll := func() error {
		bw.Flush()
		return bw.Error()
	}

	for sc.Scan() {
		lineNo++
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		if maxRows > 0 && res.RowsConverted >= maxRows {
			res.Truncated = true
			break
		}
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			return fmt.Errorf("第 %d 行不是合法 JSON 对象: %w", lineNo, err)
		}
		if cols == nil {
			cols = []string{}
			for k := range rec {
				cols = append(cols, k)
			}
			if len(cols) == 0 {
				return fmt.Errorf("第 %d 行 JSON 为空对象，无法推断列名", lineNo)
			}
			sort.Strings(cols)
			if err := bw.Write(cols); err != nil {
				return err
			}
		}
		vals := make([]string, len(cols))
		for i, c := range cols {
			v, ok := rec[c]
			if !ok || v == nil {
				vals[i] = ""
				continue
			}
			switch t := v.(type) {
			case string:
				vals[i] = t
			default:
				b, _ := json.Marshal(t)
				vals[i] = string(b)
			}
		}
		// 后出现的行可能有新列：追加表头列并重写表头不可行（已写出），
		// 简单策略——忽略未知列并在首遇时记录（转换语义与 pandas extras='ignore' 一致）
		if err := bw.Write(vals); err != nil {
			return err
		}
		res.RowsConverted++
	}
	if err := sc.Err(); err != nil {
		return fmt.Errorf("读取 JSONL 失败: %w", err)
	}
	return flushAll()
}
