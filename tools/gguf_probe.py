"""Безопасная проверка GGUF перед регистрацией модели (TT-111; ТЗ LEARN-02 REQ-DEV-07, SEC OBS-10, TT3 «карантин GGUF»).

GGUF — недоверенный вход: файл не исполняется и не загружается в runtime, читается только заголовок
и метаданные, с жёсткими пределами (число ключей, длина строк и массивов, общий объём прочитанного).
Нарушение формата — отказ с причиной, а не падение. Большие массивы (словарь токенизатора) не
сохраняются, только их длина. Хеш SHA-256 всего файла — по требованию (`--sha`): это идентичность
весов для model_config_id.
"""
import hashlib
import json
import os
import struct
import sys

MAGIC = b'GGUF'
VERSIONS = (2, 3)
MAX_KV = 20_000
MAX_STR = 1 << 20
MAX_ARRAY = 10_000_000
MAX_META_BYTES = 256 << 20
MAX_TENSORS = 1_000_000
FILE_TYPES = {0: 'F32', 1: 'F16', 2: 'Q4_0', 3: 'Q4_1', 7: 'Q8_0', 8: 'Q5_0', 9: 'Q5_1', 10: 'Q2_K', 11: 'Q3_K_S',
              12: 'Q3_K_M', 13: 'Q3_K_L', 14: 'Q4_K_S', 15: 'Q4_K_M', 16: 'Q5_K_S', 17: 'Q5_K_M', 18: 'Q6_K',
              19: 'IQ2_XXS', 20: 'IQ2_XS', 24: 'IQ1_S', 25: 'IQ4_NL', 26: 'IQ3_S', 28: 'IQ4_XS', 30: 'IQ1_M', 32: 'BF16'}
SCALAR = {0: '<B', 1: '<b', 2: '<H', 3: '<h', 4: '<I', 5: '<i', 6: '<f', 7: '<?', 10: '<Q', 11: '<q', 12: '<d'}
KEEP = ('general.architecture', 'general.name', 'general.file_type', 'general.quantization_version', 'general.license',
        'general.basename', 'general.size_label', 'tokenizer.ggml.model', 'tokenizer.chat_template',
        'general.type', 'adapter.type', 'adapter.lora.alpha')


class BadGGUF(ValueError):
    pass


class _Reader:
    def __init__(self, fh):
        self.fh = fh; self.read_bytes = 0

    def take(self, n):
        self.read_bytes += n
        if self.read_bytes > MAX_META_BYTES:
            raise BadGGUF('метаданные больше %d МБ' % (MAX_META_BYTES >> 20))
        b = self.fh.read(n)
        if len(b) != n:
            raise BadGGUF('файл обрывается внутри метаданных')
        return b

    def num(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def string(self):
        n = self.num('<Q')
        if n > MAX_STR:
            raise BadGGUF('строка метаданных длиной %d' % n)
        return self.take(n).decode('utf-8', 'replace')

    def value(self, typ, keep=True):
        if typ in SCALAR:
            return self.num(SCALAR[typ])
        if typ == 8:
            return self.string()
        if typ == 9:
            sub, n = self.num('<I'), self.num('<Q')
            if n > MAX_ARRAY:
                raise BadGGUF('массив метаданных длиной %d' % n)
            if sub == 9:
                raise BadGGUF('вложенные массивы не поддерживаются')
            if sub in SCALAR and sub != 7:
                size = struct.calcsize(SCALAR[sub]); self.take(size * n)
            else:
                for _ in range(n):
                    self.value(sub, keep=False)
            return dict(array=n, type=sub)
        raise BadGGUF('неизвестный тип значения %s' % typ)


MAX_FILE_BYTES = 64 << 30          # 64 ГиБ: больше на этой машине не поместится ни в VRAM, ни в ОЗУ


def quarantine(path, max_bytes=MAX_FILE_BYTES):
    """Карантин до чтения (SEC-015): локальный обычный файл .gguf разумного размера, не ссылка и не сетевой путь.
    Возвращает нормализованный путь; BadGGUF — файл не принят. Веса здесь не загружаются: контроллер только
    читает заголовок, исполняет модель отдельный процесс рантайма."""
    p = str(path or '')
    if p.startswith(('\\\\', '//')) or p.startswith('\\\\?\\'):
        raise BadGGUF('сетевой или служебный путь не принимается: только локальный диск')
    if not p.lower().endswith('.gguf'):
        raise BadGGUF('ожидается файл .gguf')
    if os.path.islink(p):
        raise BadGGUF('символическая ссылка не принимается: укажите сам файл')
    real = os.path.realpath(p)
    if os.path.normcase(real) != os.path.normcase(os.path.abspath(p)):
        raise BadGGUF('путь ведёт через ссылку или junction: %s' % real)
    if not os.path.isfile(real):
        raise BadGGUF('файл не найден')
    if os.name == 'nt':
        try:
            import ctypes
            drive = os.path.splitdrive(real)[0] + '\\'
            if ctypes.windll.kernel32.GetDriveTypeW(drive) == 4:      # DRIVE_REMOTE
                raise BadGGUF('сетевой диск %s не принимается' % drive)
        except (AttributeError, OSError):
            pass
    size = os.path.getsize(real)
    if size > max_bytes:
        raise BadGGUF('файл %.1f ГиБ больше предела %d ГиБ' % (size / (1 << 30), max_bytes >> 30))
    if size < 64:
        raise BadGGUF('файл слишком мал для GGUF')
    return real


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b''):
            h.update(chunk)
    return 'sha256:' + h.hexdigest()


def inspect(path, sha=False):
    """Заголовок и ключевые метаданные GGUF. BadGGUF — файл не принят (карантин)."""
    path = quarantine(path)
    size = os.path.getsize(path)
    with open(path, 'rb') as fh:
        r = _Reader(fh)
        if r.take(4) != MAGIC:
            raise BadGGUF('нет сигнатуры GGUF')
        version = r.num('<I')
        if version not in VERSIONS:
            raise BadGGUF('версия GGUF %s не поддерживается' % version)
        tensors, kvs = r.num('<Q'), r.num('<Q')
        if tensors > MAX_TENSORS or kvs > MAX_KV:
            raise BadGGUF('неправдоподобный заголовок: тензоров %d, ключей %d' % (tensors, kvs))
        meta, ctx = {}, None
        for _ in range(kvs):
            key = r.string(); typ = r.num('<I'); val = r.value(typ)
            if key in KEEP:
                meta[key] = val[:2000] if isinstance(val, str) else val
            if key.endswith('.context_length') and isinstance(val, int):
                ctx = val
            for suffix in ('.block_count', '.embedding_length', '.attention.head_count'):
                if key.endswith(suffix) and isinstance(val, int):
                    meta[suffix.strip('.')] = val
    ft = meta.get('general.file_type')
    out = dict(path=os.path.abspath(path), bytes=size, version=version, tensors=tensors, kv=kvs,
               architecture=meta.get('general.architecture'), name=meta.get('general.name'),
               quantization=FILE_TYPES.get(ft, str(ft) if ft is not None else None), context_length=ctx,
               license=meta.get('general.license'), chat_template_sha256=hashlib.sha256(
                   meta['tokenizer.chat_template'].encode('utf-8')).hexdigest()[:16] if meta.get('tokenizer.chat_template') else None,
               meta={k: v for k, v in meta.items() if k != 'tokenizer.chat_template'})
    if sha:
        out['weight_sha256'] = file_sha256(path)
    return out


def fits(info, vram_mb, ram_mb=None):
    """Грубая оценка: веса + 15% на KV-кеш и буферы. Только подсказка — решает замер (TT3-005)."""
    need = info['bytes'] * 1.15 / (1 << 20)
    return dict(need_mb=round(need), vram_mb=vram_mb, gpu_full=bool(vram_mb and need <= vram_mb * 0.95),
                note='полностью в VRAM' if vram_mb and need <= vram_mb * 0.95 else 'частичная выгрузка слоёв на CPU (offload)')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print('gguf_probe.py <файл.gguf> [--sha]'); return 2
    try:
        print(json.dumps(inspect(argv[0], sha='--sha' in argv), ensure_ascii=False, indent=1))
    except (BadGGUF, OSError) as e:
        print('GGUF не принят: %s' % e); return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
