"""
工具名称: rag_query
功能: 从本地知识库检索相关文档片段（BM25 + 向量混合召回 → RRF 融合 → cross-encoder 精排）
完全本地运行

架构（2026-10-06 重写，替代原 all-MiniLM 纯向量实现）：
    索引  : 文档加载 → 结构化切分（md 标题 / 代码 def-class / 其余段落）
            → 超长段递归降级（空行→换行→句号→空格） → token 预算累积 + overlap
            → bge-m3 向量化 → ChromaDB(cosine) ｜同步落一份 BM25 语料
    检索  : query →[BM25 粗召回] ∥ [向量粗召回] → RRF 融合 → cross-encoder 精排 → top_k
    HyDE  : 可选（默认关），先用 LLM 生成假想答案，用答案去检索

全部参数来自 config.yaml 的 `rag:` 段（见 load_rag_config）。
重依赖（chromadb / sentence_transformers / torch）一律**延迟导入**，避免拖慢 agent 进程启动。
"""

import os
import re
import json
import time
import hashlib
import logging
import threading
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any

# ===== 项目根 =====
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
HF_CACHE_DIR = PROJECT_ROOT / "hf_cache"


def _configure_local_model_env() -> None:
    """把 HF 缓存指到项目内、默认离线。只填空（setdefault），不覆盖已有配置。

    沿用旧实现的审计修正：这段曾在 import 期无条件改写 os.environ，
    现在收进函数、调用一次即止（仍早于任何模型加载）。
    离线开关很重要 —— HF 主站本机不通，不关会白等连接超时。
    """
    os.environ.setdefault('HF_HOME', str(HF_CACHE_DIR))
    os.environ.setdefault('TRANSFORMERS_CACHE', str(HF_CACHE_DIR))
    os.environ.setdefault('HUGGINGFACE_HUB_CACHE', str(HF_CACHE_DIR))
    os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')
    os.environ.setdefault('HF_HUB_OFFLINE', '1')
    os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')


_configure_local_model_env()
logger = logging.getLogger(__name__)


# ========== 0. 配置 ==========
# config.yaml 的 rag: 段；缺键一律回落到这里的默认值（向后兼容）
_DEFAULT_RAG_CONFIG: Dict[str, Any] = {
    # 索引
    "chunk_size": 512,
    "chunk_overlap": 80,
    "embedding_model": "BAAI/bge-m3",
    "knowledge_dir": "agent_knowledge_base",
    "encode_batch_size": 16,
    # 检索
    "recall_k": 30,
    "rrf_k": 60,
    "final_k": 3,
    "reranker_model": "cross-encoder/ms-marco-MiniLM-L6-v2",
    "rerank_pool": 60,
    "min_score": 0.0,
    "enable_hyde": False,
    # 向量库
    "vector_store": "chroma",
    "chroma_path": "chroma_db",
    "collection_name": "ab_knowledge",
    "bm25_path": "chroma_db/bm25_corpus.json",
    # 设备：auto = 有 CUDA 用 CUDA，否则 CPU
    "device": "auto",
}


def load_rag_config() -> Dict[str, Any]:
    """读取 config.yaml 的 rag: 段，与默认值合并（缺键回落）。失败不抛，返回默认。"""
    cfg = dict(_DEFAULT_RAG_CONFIG)
    try:
        import yaml
        cfg_path = PROJECT_ROOT / "config.yaml"
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            user_cfg = loaded.get("rag") or {}
            if isinstance(user_cfg, dict):
                cfg.update({k: v for k, v in user_cfg.items() if v is not None})
    except Exception as e:                       # 配置坏了不该让工具整个不可用
        logger.warning(f"读取 rag 配置失败，使用默认值: {e}")
    return cfg


def _abs_path(rel: str) -> str:
    """配置里的相对路径一律相对项目根解析。"""
    p = Path(rel)
    return str(p if p.is_absolute() else (PROJECT_ROOT / p))


def _resolve_device(pref: str) -> Optional[str]:
    """auto → 有 CUDA 用 cuda，否则 cpu。"""
    pref = (pref or "auto").lower()
    if pref in ("cpu", "cuda"):
        return pref
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _doc_hash(text: str) -> str:
    """文本内容哈希。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


def _file_hash(path: str) -> str:
    """文件级哈希（增量与切分缓存的判据）。

    直接读字节 —— 比「先解析 PDF 再对全文做哈希」快一个数量级，
    让续跑轮可以完全跳过文档解析。
    """
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()[:16]


# ========== 1. 文档加载 ==========
SUPPORTED_EXTS = {".md", ".markdown", ".txt", ".py", ".pdf", ".docx"}


def _clean_text(text: str) -> str:
    """轻度清洗：去零宽字符、压缩 3+ 连续空行、去行尾空白。不破坏 Markdown 结构。"""
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_document(path: str) -> Optional[Dict[str, Any]]:
    """加载单个文档 → {"source", "kind", "blocks": [{"page": int, "text": str}]}

    block 是"天然带页码/位置的最小单元"：
        pdf  → 一页一个 block（page = 真实页码）
        docx → 一页一个 block（page = 段落序号），保留分页线索
        其它 → 整篇一个 block（page = 1），由 Chunker 按 kind 结构化切分
    不支持 / 读失败 → None。
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        return None
    try:
        if ext == ".pdf":
            import pymupdf
            doc = pymupdf.open(str(p))
            blocks = []
            for i, page in enumerate(doc):
                t = _clean_text(page.get_text())
                if t:
                    blocks.append({"page": i + 1, "text": t})
            doc.close()
            return {"source": p.name, "kind": "pdf", "blocks": blocks}

        if ext == ".docx":
            from docx import Document
            d = Document(str(p))
            blocks, buf = [], []
            for i, para in enumerate(d.paragraphs):
                t = _clean_text(para.text)
                if t:
                    buf.append(t)
            if buf:
                blocks.append({"page": 1, "text": "\n\n".join(buf)})
            return {"source": p.name, "kind": "docx", "blocks": blocks}

        # 纯文本类（md / txt / py）—— 读法兼容 GBK 兜底
        try:
            raw = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw = p.read_text(encoding="gbk", errors="replace")
        kind = "markdown" if ext in (".md", ".markdown") else ("code" if ext == ".py" else "text")
        return {"source": p.name, "kind": kind,
                "blocks": [{"page": 1, "text": _clean_text(raw)}]}
    except Exception as e:
        logger.warning(f"加载文档失败 {p.name}: {e}")
        return None


def iter_documents(directory: str) -> List[str]:
    """递归列出目录下受支持的文档（返回绝对路径列表）。"""
    out: List[str] = []
    base = Path(directory)
    if not base.exists():
        return out
    for fp in sorted(base.rglob("*")):
        if fp.is_file() and fp.suffix.lower() in SUPPORTED_EXTS:
            out.append(str(fp))
    return out


# ========== 2. 分块 ==========
# 递归降级的切分符（优先级从高到低）：空行 → 换行 → 句末标点 → 空白
_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", " ", "\t"]
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_CODE_DEF = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")


class Chunker:
    """结构化切分 + 递归降级 + token 预算累积（含 overlap）。

    token 计数用与 embedding 同源的 tokenizer（bge-m3），保证「512 token」是真 token 数，
    而不是字符数近似。
    """

    def __init__(self, tokenizer, chunk_size: int = 512, overlap: int = 80):
        self.tok = tokenizer
        self.chunk_size = max(64, int(chunk_size))
        self.overlap = max(0, min(int(overlap), self.chunk_size // 2))

    def count(self, text: str) -> int:
        if not text:
            return 0
        try:
            return len(self.tok.encode(text, add_special_tokens=False))
        except Exception:
            return max(1, len(text) // 2)          # 兜底：中文约 1.8 字符/token

    # ---- 结构化切分：按文档类型切成语义单元 ----
    def split_sections(self, text: str, kind: str) -> List[str]:
        if kind == "markdown":
            return self._split_markdown(text)
        if kind == "code":
            return self._split_code(text)
        return self._split_paragraphs(text)

    @staticmethod
    def _split_markdown(text: str) -> List[str]:
        """按标题切；标题保留在所属 chunk 的开头。标题前的引言单独成段。"""
        sections, cur = [], []
        for line in text.split("\n"):
            if _MD_HEADING.match(line) and cur:
                sections.append("\n".join(cur))
                cur = [line]
            else:
                cur.append(line)
        if cur:
            sections.append("\n".join(cur))
        return [s.strip() for s in sections if s.strip()]

    @staticmethod
    def _split_code(text: str) -> List[str]:
        """按顶层 def / class 切；非代码引言（import 段等）单独成块。"""
        sections, cur = [], []
        for line in text.split("\n"):
            is_def = bool(_CODE_DEF.match(line)) and not line.startswith((" ", "\t"))
            if is_def and cur:
                sections.append("\n".join(cur))
                cur = [line]
            else:
                cur.append(line)
        if cur:
            sections.append("\n".join(cur))
        return [s.strip() for s in sections if s.strip()]

    @staticmethod
    def _split_paragraphs(text: str) -> List[str]:
        return [s.strip() for s in re.split(r"\n\s*\n", text) if s.strip()]

    # ---- 超长单元的递归降级 ----
    def recursive_split(self, text: str, seps: Optional[List[str]] = None) -> List[str]:
        """把超长文本按分隔符逐级降级切分，尽量保住语义完整；都不行才硬切。"""
        if self.count(text) <= self.chunk_size:
            return [text] if text.strip() else []
        seps = _SEPARATORS if seps is None else seps
        if not seps:
            return self._hard_split(text)

        sep, rest = seps[0], seps[1:]
        out: List[str] = []
        buf, buf_tok = "", 0
        for part in text.split(sep):
            piece = part + sep
            pt = self.count(piece)          # 每片只分词一次（原先每轮重算 buf+piece = O(n^2)）
            if buf_tok + pt <= self.chunk_size:
                buf += piece
                buf_tok += pt
                continue
            if buf:
                out.append(buf)
                buf, buf_tok = "", 0
            if pt > self.chunk_size:
                out.extend(self.recursive_split(piece, rest))   # 单段仍超长 → 再降一级
            else:
                buf, buf_tok = piece, pt
        if buf:
            out.append(buf)
        return [s for s in out if s.strip()]

    def _hard_split(self, text: str) -> List[str]:
        """最后兜底：按 token 上限二分硬切（所有分隔符都失效时）。
        不用固定字符步长 —— 同样的字符数，英文/数字的 token 密度远高于中文。"""
        out, start = [], 0
        n = len(text)
        while start < n:
            lo, hi, best = 1, n - start, 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if self.count(text[start:start + mid]) <= self.chunk_size:
                    best, lo = mid, mid + 1
                else:
                    hi = mid - 1
            piece = text[start:start + best].strip()
            if piece:
                out.append(piece)
            start += best
        return out

    # ---- 累积（含 overlap）----
    def _accumulate(self, pieces: List[str]) -> List[str]:
        chunks: List[str] = []
        cur: List[str] = []
        cur_tok = 0
        for pc in pieces:
            t = self.count(pc)
            if cur and cur_tok + t > self.chunk_size:
                chunks.append("".join(cur))
                # 回退 overlap：从尾部按"段"累加，直到凑够 overlap token
                tail, tail_tok = [], 0
                for s in reversed(cur):
                    st = self.count(s)
                    if tail_tok >= self.overlap or tail_tok + st + t > self.chunk_size:
                        break
                    tail.insert(0, s)
                    tail_tok += st
                cur, cur_tok = tail, tail_tok
            cur.append(pc)
            cur_tok += t
        if cur:
            chunks.append("".join(cur))
        return [c.strip() for c in chunks if c.strip()]

    # ---- 主流程 ----
    def chunk_document(self, doc: Dict[str, Any],
                       doc_hash: Optional[str] = None) -> List[Dict[str, Any]]:
        """文档 → chunk 列表（每个 chunk 带完整元数据）。"""
        source = doc["source"]
        kind = doc["kind"]
        dh = doc_hash or _doc_hash("\n".join(b["text"] for b in doc["blocks"]))
        out: List[Dict[str, Any]] = []
        idx = 0
        for block in doc["blocks"]:
            sections = self.split_sections(block["text"], kind)
            # 结构化单元（Markdown 标题 / 代码 def-class）是**硬边界**：不跨单元合并，
            # 否则 section 元数据会把 A 节的内容标成 B 节的来源；段落则允许合并凑满预算。
            groups = [[s] for s in sections] if kind in ("markdown", "code") else [sections]
            for group in groups:
                pieces: List[str] = []
                for sec in group:
                    if self.count(sec) > self.chunk_size:
                        pieces.extend(self.recursive_split(sec))
                    else:
                        pieces.append(sec + "\n\n")
                for text in self._accumulate(pieces):
                    out.append({
                        "chunk_id": f"{source}#{idx}",
                        "text": text,
                        "source": source,
                        "page": int(block.get("page", -1)),
                        "section": self._heading_of(text) if kind == "markdown" else "",
                        "doc_hash": dh,
                    })
                    idx += 1
        return out

    @staticmethod
    def _heading_of(text: str) -> str:
        """取 chunk 内首个 Markdown 标题（作为 section 溯源）。"""
        for line in text.split("\n"):
            m = _MD_HEADING.match(line)
            if m:
                return m.group(2).strip()[:120]
        return ""


# ========== 3. 分词 与 BM25 ==========
_WORD_RE = re.compile(r"^[\w\u4e00-\u9fff]+$")


def tokenize(text: str) -> List[str]:
    """中文优先分词（jieba），保留中英数字词，丢弃标点。"""
    import jieba
    out = []
    for w in jieba.lcut((text or "").lower()):
        w = w.strip()
        if w and _WORD_RE.match(w):
            out.append(w)
    return out


class BM25Store:
    """BM25 语料与索引。语料（chunk 文本 + 元数据）落 JSON，索引内存构建。

    Chroma 不管 BM25，所以这里单独维护一份；两路结果靠 chunk_id 对齐。
    """

    def __init__(self, corpus_path: str):
        self.corpus_path = corpus_path
        self.chunks: List[Dict[str, Any]] = []
        self._bm25 = None
        self._lock = threading.Lock()

    def load(self) -> "BM25Store":
        if os.path.exists(self.corpus_path):
            try:
                with open(self.corpus_path, "r", encoding="utf-8") as f:
                    self.chunks = json.load(f).get("chunks", [])
            except Exception as e:
                logger.warning(f"BM25 语料读取失败: {e}")
                self.chunks = []
        return self

    def save(self, chunks: List[Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.corpus_path), exist_ok=True)
        payload = {"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "chunks": chunks}
        tmp = self.corpus_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, self.corpus_path)          # 原子替换：半截文件永不留在磁盘上
        self.chunks = chunks
        self._bm25 = None

    def _ensure_index(self):
        if self._bm25 is None:
            with self._lock:
                if self._bm25 is None:
                    from rank_bm25 import BM25Okapi
                    self._bm25 = BM25Okapi([tokenize(c.get("text", "")) for c in self.chunks])
        return self._bm25

    def search(self, query: str, k: int) -> List[Tuple[str, float]]:
        if not self.chunks:
            return []
        bm25 = self._ensure_index()
        scores = bm25.get_scores(tokenize(query))
        order = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
        return [(self.chunks[i]["chunk_id"], float(scores[i])) for i in order if scores[i] > 0]


# ========== 4. 索引（向量 + BM25 + 增量） ==========
class RAGIndex:
    """懒加载的索引器/检索器单例。

    分块与向量化只在 build() 里发生；检索侧只加载 BM25 语料 + 向量库 + 两个模型。
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.cfg = cfg or load_rag_config()
        self.chunk_size = int(self.cfg["chunk_size"])
        self.overlap = int(self.cfg["chunk_overlap"])
        self._client = None
        self._coll = None
        self._model = None
        self._reranker = None
        self._tokenizer = None
        self._bm25: Optional[BM25Store] = None
        self._lock = threading.RLock()      # 保护懒加载：宿主可能并发发起工具调用

    # ---- 路径 ----
    @property
    def chroma_path(self) -> str:
        return _abs_path(self.cfg["chroma_path"])

    @property
    def bm25_path(self) -> str:
        return _abs_path(self.cfg["bm25_path"])

    @property
    def manifest_path(self) -> str:
        return os.path.join(os.path.dirname(self.bm25_path), "index_manifest.json")

    # ---- 懒加载 ----
    def _ensure_collection(self):
        if self._coll is not None:
            return self._coll
        with self._lock:
            if self._coll is not None:
                return self._coll
            import chromadb
            from chromadb.config import Settings
            os.makedirs(self.chroma_path, exist_ok=True)
            self._client = chromadb.PersistentClient(
                path=self.chroma_path, settings=Settings(anonymized_telemetry=False))
            # embedding_function=None：向量一律由 bge-m3 显式算出后传入，
            # 绝不让 Chroma 用它的默认模型重算一遍（浪费且不一致）。
            self._coll = self._client.get_or_create_collection(
                name=self.cfg["collection_name"],
                metadata={"hnsw:space": "cosine"},
                embedding_function=None,
            )
        return self._coll

    def _ensure_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer
                    device = _resolve_device(self.cfg.get("device", "auto"))
                    self._model = SentenceTransformer(self.cfg["embedding_model"], device=device)
        return self._model

    def _ensure_tokenizer(self):
        if self._tokenizer is None:
            with self._lock:
                if self._tokenizer is None:
                    from transformers import AutoTokenizer
                    self._tokenizer = AutoTokenizer.from_pretrained(self.cfg["embedding_model"])
        return self._tokenizer

    def _ensure_reranker(self):
        if self._reranker is None:
            with self._lock:
                if self._reranker is None:
                    from sentence_transformers import CrossEncoder
                    device = _resolve_device(self.cfg.get("device", "auto"))
                    self._reranker = CrossEncoder(self.cfg["reranker_model"], device=device)
        return self._reranker

    def _bm25_store(self) -> BM25Store:
        if self._bm25 is None:
            with self._lock:
                if self._bm25 is None:
                    self._bm25 = BM25Store(self.bm25_path).load()
        return self._bm25

    # ---- manifest（增量 + 断点续传的进度真相）----
    def _load_manifest(self) -> Dict[str, Any]:
        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    @property
    def chunks_cache_path(self) -> str:
        return os.path.join(os.path.dirname(self.bm25_path), "chunks_cache.json")

    def _load_cached_chunks(self, source: str, file_hash: str):
        """取切分缓存（命中则完全跳过文档解析 + 分块，续跑轮的关键提速）。"""
        try:
            if not os.path.exists(self.chunks_cache_path):
                return None
            with open(self.chunks_cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            rec = cache.get(source) or {}
            if rec.get("hash") == file_hash and rec.get("chunks"):
                return rec["chunks"]
        except Exception:
            pass
        return None

    def _save_cached_chunks(self, source: str, file_hash: str,
                            chunks: List[Dict[str, Any]]) -> None:
        cache = {}
        try:
            if os.path.exists(self.chunks_cache_path):
                with open(self.chunks_cache_path, "r", encoding="utf-8") as f:
                    cache = json.load(f)
        except Exception:
            cache = {}
        cache[source] = {"hash": file_hash, "chunks": chunks}
        os.makedirs(os.path.dirname(self.chunks_cache_path), exist_ok=True)
        tmp = self.chunks_cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, self.chunks_cache_path)

    def _save_manifest(self, m: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.manifest_path), exist_ok=True)
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.manifest_path)

    # ---- 索引主流程 ----
    def build(self, directory: Optional[str] = None,
              budget_seconds: Optional[float] = None,
              rebuild: bool = False,
              verbose: bool = True) -> Dict[str, Any]:
        """增量构建索引。

        budget_seconds: 软预算（秒）。到点**主动退出**并保存进度，下次调用从断点续跑
                        —— 这是绕开工具 120s 上限的机制，进度以「已写入的 chunk」为准。
        rebuild       : 清空 collection 与 manifest，全量重建。
        """
        t0 = time.time()
        if rebuild:
            self.reset()
        coll = self._ensure_collection()
        chunker = None                       # 命中分块缓存时**完全不加载 tokenizer**
        manifest = self._load_manifest()
        report: Dict[str, Any] = {"indexed": [], "skipped": [], "partial": [],
                                  "deleted": [], "chunks_added": 0, "errors": []}

        directory = directory or _abs_path(self.cfg["knowledge_dir"])
        batch_size = int(self.cfg.get("encode_batch_size", 16))

        def _log(msg):
            if verbose:
                print(msg, flush=True)

        for fp in iter_documents(directory):
            src = os.path.basename(fp)
            h = _file_hash(fp)               # 只读字节：续跑轮无需解析文档即可算判据
            rec = manifest.get(src) or {}

            if rec.get("hash") == h and rec.get("complete"):
                report["skipped"].append(src)
                _log(f"[SKIP] {src}（未变化）")
                continue

            if rec and rec.get("hash") != h:
                coll.delete(where={"source": src})       # 文档变了 → 旧 chunk 先清干净
                report["deleted"].append(src)
                _log(f"[DEL ] {src}（内容已变化，旧块清除）")

            chunks = self._load_cached_chunks(src, h)
            if chunks is None:
                doc = load_document(fp)
                if not doc:
                    continue
                if chunker is None:
                    chunker = Chunker(self._ensure_tokenizer(), self.chunk_size, self.overlap)
                chunks = chunker.chunk_document(doc, doc_hash=h)
                self._save_cached_chunks(src, h, chunks)
                _log(f"[SPLIT] {src} → {len(chunks)} 块（分块结果已缓存）")
            else:
                _log(f"[CACHE] {src} → 复用缓存分块（{len(chunks)} 块）")
            if not chunks:
                continue

            # 断点续传核心：只补插缺失的 chunk（id 稳定 = source#序号）。
            # 按 source 分页取回已存在 id —— 比逐个 id 查更快，也不会撞上单次查询长度限制。
            existing = set()
            offset = 0
            while True:
                got = coll.get(where={"source": src}, limit=1000, offset=offset, include=[])
                page = got.get("ids") or []
                existing.update(page)
                if len(page) < 1000:
                    break
                offset += len(page)
            todo = [c for c in chunks if c["chunk_id"] not in existing]

            model = self._ensure_model()
            for i in range(0, len(todo), batch_size):
                # 预算检查放在**批次开始前**：若放在批次结束后，会多跑整整一批才退出，
                # 实测就是因此被工具的 120s 上限强杀（进度虽在 chroma 里，但 manifest 没落盘）。
                if budget_seconds and (time.time() - t0) > budget_seconds:
                    manifest[src] = {"hash": h, "complete": False, "chunks": len(chunks)}
                    self._save_manifest(manifest)
                    report["partial"].append(src)
                    report["resume_hint"] = (f"{len(existing) + report['chunks_added']}"
                                             f"/{len(chunks)}")
                    _log(f"[PART] {src} 预算到点，已存进度（{len(existing)}/{len(chunks)}），下次续跑")
                    return report
                batch = todo[i:i + batch_size]
                embs = model.encode([c["text"] for c in batch],
                                    batch_size=batch_size, normalize_embeddings=True)
                coll.add(
                    ids=[c["chunk_id"] for c in batch],
                    embeddings=[list(map(float, v)) for v in embs],
                    documents=[c["text"] for c in batch],
                    metadatas=[{"source": c["source"], "page": int(c["page"]),
                                "section": c["section"], "chunk_id": c["chunk_id"],
                                "doc_hash": c["doc_hash"],
                                "timestamp": int(time.time())} for c in batch],
                )
                report["chunks_added"] += len(batch)

            manifest[src] = {"hash": h, "complete": True, "chunks": len(chunks)}
            self._save_manifest(manifest)
            report["indexed"].append(src)
            _log(f"[OK  ] {src} → {len(chunks)} 块（本次新增 {len(todo)}）")

        # 全部完成 → 从向量库权威导出 BM25 语料（天然与向量侧一致）
        self.sync_bm25()
        report["bm25_chunks"] = len(self._bm25_store().chunks)
        _log(f"[BM25] 语料同步完成：{report['bm25_chunks']} 块，用时 {time.time() - t0:.1f}s")
        return report

    def sync_bm25(self) -> int:
        """从 ChromaDB 全量导出 chunk 文本 + 元数据，重建 BM25 语料。"""
        coll = self._ensure_collection()
        got = coll.get(include=["documents", "metadatas"])
        chunks = []
        for cid, text, meta in zip(got.get("ids") or [], got.get("documents") or [],
                                   got.get("metadatas") or []):
            m = dict(meta or {})
            m["chunk_id"] = cid
            m["text"] = text or ""
            chunks.append(m)
        BM25Store(self.bm25_path).save(chunks)
        self._bm25 = None                       # 让下次检索重建内存索引
        return len(chunks)

    def reset(self) -> None:
        """清空 collection + manifest + BM25 语料（全量重建用）。"""
        import chromadb
        from chromadb.config import Settings
        os.makedirs(self.chroma_path, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=self.chroma_path, settings=Settings(anonymized_telemetry=False))
        try:
            self._client.delete_collection(self.cfg["collection_name"])
        except Exception:
            pass
        self._coll = None
        for f in (self.manifest_path, self.bm25_path, self.chunks_cache_path):
            if os.path.exists(f):
                os.remove(f)
        self._bm25 = None

    def stats(self) -> Dict[str, Any]:
        coll = self._ensure_collection()
        return {"collection": self.cfg["collection_name"],
                "vectors": coll.count(),
                "bm25_chunks": len(self._bm25_store().chunks),
                "manifest": len(self._load_manifest())}


# ========== 5. 混合检索：双路召回 → RRF 融合 → cross-encoder 精排 ==========
def rrf_fuse(rankings: List[List[Tuple[str, float]]], k: int = 60) -> List[Tuple[str, float]]:
    """Reciprocal Rank Fusion。

    score(doc) = Σ 1/(k + rank_i(doc))，rank 从 1 起。
    只吃「名次」，不吃各路原始分数量纲 —— 这正是它能融合 BM25 与 cosine 两种
    完全不同分数体系的原因。同一 chunk 在两路都出现时分数相加（自动去重）。
    """
    scores: Dict[str, float] = {}
    for ranking in rankings:
        for rank, (cid, _raw) in enumerate(ranking, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


class RAGRetriever:
    """检索器：BM25 ∥ 向量 → RRF → 精排。"""

    def __init__(self, index: RAGIndex):
        self.index = index

    # ---- 单路召回 ----
    def _bm25_recall(self, query: str, k: int) -> List[Tuple[str, float]]:
        return self.index._bm25_store().search(query, k)

    def _vector_recall(self, query: str, k: int) -> List[Tuple[str, float]]:
        coll = self.index._ensure_collection()
        if coll.count() == 0:
            return []
        model = self.index._ensure_model()
        qv = model.encode([query], normalize_embeddings=True)[0]
        res = coll.query(query_embeddings=[list(map(float, qv))],
                         n_results=min(k, max(1, coll.count())),
                         include=["distances"])
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        # cosine 空间：distance = 1 - cosine_sim
        return [(cid, 1.0 - float(d)) for cid, d in zip(ids, dists)]

    # ---- 取回 chunk 详情（以向量库为权威源）----
    def _fetch(self, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        if not ids:
            return {}
        coll = self.index._ensure_collection()
        got = coll.get(ids=ids, include=["documents", "metadatas"])
        out = {}
        for cid, text, meta in zip(got.get("ids") or [], got.get("documents") or [],
                                   got.get("metadatas") or []):
            out[cid] = {"chunk_id": cid, "text": text or "", **(meta or {})}
        return out

    # ---- 精排 ----
    def _rerank(self, query: str, cand_ids: List[str],
                details: Dict[str, Dict[str, Any]], top_k: int) -> List[Dict[str, Any]]:
        """cross-encoder 对**融合后的统一候选列表**打分（不是分别对两路打）。"""
        pairs, keep = [], []
        for cid in cand_ids:
            d = details.get(cid)
            if d:
                pairs.append((query, d["text"]))
                keep.append(cid)
        if not pairs:
            return []
        ce = self.index._ensure_reranker()
        raw = ce.predict(pairs)
        scored = []
        for cid, s in zip(keep, raw):
            item = dict(details[cid])
            # 直接用 cross-encoder 的原始分：它的 logits 跨度大（约 -10 ~ +15），
            # 过 sigmoid 会整片饱和到 1.000，**实测把全部结果的区分度抹平了**
            # （首版就是这样：三条结果全是 1.000，看不出谁更相关）。
            item["score"] = float(s)
            scored.append(item)
        scored.sort(key=lambda x: -x["score"])
        return scored[:top_k]

    # ---- HyDE（可选）----
    def _hyde_expand(self, query: str) -> str:
        """用 LLM 生成一段「假想答案」，拿答案去检索（默认关闭）。"""
        try:
            from dotenv import load_dotenv
            load_dotenv(PROJECT_ROOT / ".env")
            from openai import OpenAI
            client = OpenAI(api_key=os.getenv("LLM_API_KEY"),
                            base_url=os.getenv("LLM_BASE_URL"))
            prompt = ("请针对下面的问题，用中文写一段 150 字以内的、"
                      "像是知识库里会出现的解答文字。只输出这段文字，不要解释：\n\n" + query)
            resp = client.chat.completions.create(
                model=os.getenv("LLM_MODEL"),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=3000, temperature=0.3)
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                logger.warning(
                    "HyDE 返回空内容（推理模型的 reasoning_tokens 可能吃光了 "
                    "max_tokens），回退原 query")
                return query
            return query + "\n" + text
        except Exception as e:
            logger.warning(f"HyDE 生成失败，回退原 query: {e}")
            return query

    # ---- 主检索 ----
    def search(self, query: str, top_k: Optional[int] = None,
               min_score: Optional[float] = None,
               enable_hyde: Optional[bool] = None) -> Dict[str, Any]:
        cfg = self.index.cfg
        recall_k = int(cfg.get("recall_k", 30))
        rrf_k = int(cfg.get("rrf_k", 60))
        final_k = int(top_k or cfg.get("final_k", 3))
        pool_k = int(cfg.get("rerank_pool", 2 * recall_k) or 2 * recall_k)
        threshold = cfg.get("min_score", 0.0) if min_score is None else min_score
        use_hyde = bool(cfg.get("enable_hyde", False) if enable_hyde is None else enable_hyde)

        used_query = self._hyde_expand(query) if use_hyde else query

        # 【预热 · 关键，别删】进入并行前，两路要用的重依赖必须在**主线程**先 import 完。
        # 否则两个工作线程会「同时首次 import」numpy / torch / chromadb，实测必崩：
        #     ImportError: cannot import name 'NDArray' from partially initialized
        #     module 'numpy._typing' (most likely due to a circular import)
        # 且会连锁引发 chroma 的 KeyError: '<chroma_db 路径>' 与
        # AttributeError: 'RustBindingsAPI' object has no attribute 'bindings'
        # （三个报错是同一处初始化污染的后遗症，不是三个独立故障）。
        # 预热之后，并行段里只剩纯计算，线程就安全了。
        store = self.index._bm25_store()
        store._ensure_index()                 # rank_bm25 / numpy（含 1205 块分词建索引）
        self.index._ensure_model()            # torch / sentence_transformers
        coll = self.index._ensure_collection()
        try:
            coll.count()                      # 触发 HNSW 索引加载，别留给子线程
        except Exception:
            pass

        # 两路召回并行：BM25 走 numpy、向量走 torch，两者都会释放 GIL，所以线程是真并行。
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_bm25 = pool.submit(store.search, used_query, recall_k)
            f_vec = pool.submit(self._vector_recall, used_query, recall_k)
            bm25_hits = f_bm25.result()
            vec_hits = f_vec.result()

        fused = rrf_fuse([bm25_hits, vec_hits], rrf_k)
        pool = [cid for cid, _ in fused[:pool_k]]

        details = self._fetch(pool)
        ranked = self._rerank(used_query, pool, details, final_k * 3)
        if threshold:
            ranked = [r for r in ranked if r.get("score", 0) >= threshold]
        return {
            "query": query, "used_query": used_query, "hyde": use_hyde,
            "results": ranked[:final_k],
            "stats": {"bm25": len(bm25_hits), "vector": len(vec_hits),
                      "fused": len(fused), "pool": len(pool)},
        }


# ========== 6. 结果格式化 ==========
def format_results(payload: Dict[str, Any]) -> str:
    """把检索结果渲染成 Agent 易读、且**可溯源**的文本。"""
    results = payload.get("results") or []
    if not results:
        return ("🔍 知识库中没有检索到相关内容。\n"
                "可能原因：知识库尚未索引（先运行 build_index.py），"
                "或该问题不在知识库覆盖范围内 —— 换用 search / web_extract 更合适，"
                "也可以改写 query 后再试一次。")

    lines = [f"📚 知识库检索结果（{len(results)} 条）：", ""]
    for i, r in enumerate(results, start=1):
        page = r.get("page", -1)
        section = r.get("section", "") or ""
        if isinstance(page, int) and page > 0:
            loc = f"第 {page} 页"
        elif section:
            loc = f"章节：{section}"
        else:
            loc = "位置未知"
        lines.append(f"【{i}】来源：{r.get('source', '未知')} ｜ {loc} ｜ "
                     f"相关度：{r.get('score', 0):.3f} ｜ 块ID：{r.get('chunk_id', '')}")
        lines.append(r.get("text", "").strip())
        lines.append("")
    lines.append("（如需更多结果，可调大 top_k；结果不相关时建议改写 query 再检索一次）")
    return "\n".join(lines)


# ========== 7. 单例与工具函数 ==========
_INDEX: Optional[RAGIndex] = None
_RETRIEVER: Optional[RAGRetriever] = None
_INIT_LOCK = threading.Lock()


def get_retriever() -> RAGRetriever:
    """进程内单例（双检锁）。**不自动索引** —— 索引是显式动作（build_index.py），
    放在工具调用链里做全量索引必然撞超时。

    加锁的原因：宿主可能并发发起工具调用，而 RAGIndex 的懒加载（chroma client /
    模型）不是线程安全的 —— 两个线程同时首建会得到两份 client，甚至重演
    「SharedSystemClient 登记半成品」那类初始化损坏。
    """
    global _INDEX, _RETRIEVER
    if _RETRIEVER is None:
        with _INIT_LOCK:
            if _RETRIEVER is None:          # 双检：拿到锁后可能已被别的线程建好
                _INDEX = RAGIndex()
                _RETRIEVER = RAGRetriever(_INDEX)
    return _RETRIEVER


def rag_query(query: str, top_k: Optional[int] = None,
              min_score: Optional[float] = None,
              enable_hyde: Optional[bool] = None) -> str:
    """RAG 检索工具（供 Agent 调用）。

    参数:
        query      : 检索问题（中文可直接使用；结果不理想时**改写后再试**比调大 top_k 更有效）
        top_k      : 返回条数，默认取配置 final_k
        min_score  : 相关度下限（0~1，精排分数），低于该值的结果被过滤
        enable_hyde: 是否启用 HyDE（LLM 生成假想答案再检索），默认取配置
    返回:
        带来源（文件 / 页码或章节 / 块ID）的文档片段，或提示信息
    """
    try:
        retriever = get_retriever()
        payload = retriever.search(query, top_k=top_k,
                                   min_score=min_score, enable_hyde=enable_hyde)
        return format_results(payload)
    except FileNotFoundError as e:
        return (f"⚠️ 知识库索引不完整：{e}\n"
                "请先运行索引脚本：python agent_workspace/RAG模块实现/build_index.py")
    except Exception as e:
        logger.error(f"知识库检索失败: {e}", exc_info=True)
        return f"❌ 知识库检索失败：{type(e).__name__}: {e}"


# ========== 8. 工具 Schema ==========
rag_query_schema = {
    "type": "function",
    "function": {
        "name": "rag_query",
        "description": (
            "查询本地知识库，返回与问题最相关的文档片段。"
            "采用「BM25 关键词 + 向量语义」双路召回"
            " + cross-encoder 精排，既能命中术语原词，也能命中同义表述。"
            "\n用法要点："
            "\n  - 一次检索不理想时，**改写 query 再试**（换用文档里更可能出现的说法）；"
            "\n  - 相关度是 cross-encoder 的原始分（越高越相关，正分通常意味着真的相关，"
            "接近 0 或负分多半是没覆盖该内容），**分数只在同一次查询内可比**；"
            "\n  - 用户显式要求查询知识库时，才调用该工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索问题。用文档中可能出现的表述效果最好，可直接用中文。"
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回的片段条数，默认取配置值（3）。"
                },
                "min_score": {
                    "type": "number",
                    "description": "相关度下限，低于该值的片段会被过滤。默认取配置值。"
                },
                "enable_hyde": {
                    "type": "boolean",
                    "description": "是否启用 HyDE（先让 LLM 生成假想答案再检索）。问题表述很抽象时可开；默认关。"
                }
            },
            "required": ["query"]
        }
    }
}


# ========== 9. 命令行（调试用；索引入口见 build_index.py） ==========
def _main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="RAG 检索调试（索引构建请用 build_index.py）")
    ap.add_argument("--stats", action="store_true", help="打印索引统计")
    ap.add_argument("--query", type=str, default=None, help="执行一次检索")
    ap.add_argument("--top-k", type=int, default=None, help="返回条数")
    ap.add_argument("--hyde", action="store_true", help="启用 HyDE")
    args = ap.parse_args()

    if args.stats:
        idx = RAGIndex()
        print(json.dumps(idx.stats(), ensure_ascii=False, indent=2))
        return
    if args.query:
        print(rag_query(args.query, top_k=args.top_k, enable_hyde=args.hyde or None))
        return
    ap.print_help()


if __name__ == "__main__":
    _main()
