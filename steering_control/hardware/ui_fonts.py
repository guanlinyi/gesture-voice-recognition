"""项目内中文字体，只在当前进程注册，不安装或更改系统字体。"""
import ctypes
from ctypes.util import find_library
from functools import lru_cache
import json
from pathlib import Path
import sys
import sysconfig

FONT_DIRECTORY = Path(__file__).resolve().parent / 'fonts/noto_sans_sc'
FONT_FAMILY = 'Noto Sans CJK SC'
_TK_RUNTIME_HANDLES = ()


@lru_cache(maxsize=1)
def prepare_tk_runtime():
    """在 tkinter 首次导入前，为 Conda noxft Tk 选用同 ABI 的系统 Xft 版本。"""
    global _TK_RUNTIME_HANDLES
    if sys.platform != 'linux' or '_tkinter' in sys.modules:
        return None
    metadata = Path(sys.prefix) / 'conda-meta'
    noxft = False
    for path in metadata.glob('tk-*.json'):
        package = json.loads(path.read_text(encoding='utf-8'))
        if package.get('name') == 'tk' and str(package.get('version', '')).startswith('8.6.'):
            noxft = 'noxft' in package.get('build', '')
            break
    if not noxft:
        return None
    multiarch = sysconfig.get_config_var('MULTIARCH')
    if not multiarch:
        return None
    for base in (Path('/lib') / multiarch, Path('/usr/lib') / multiarch):
        tcl, tk = base / 'libtcl8.6.so', base / 'libtk8.6.so'
        if not tcl.is_file() or not tk.is_file() or b'libXft.so' not in tk.read_bytes():
            continue
        # 先加载同一套 Tcl/Tk；动态链接器随后为 _tkinter 复用相同 SONAME。
        # 仅改变当前进程的库选择，不替换 Conda 或系统中的任何文件。
        _TK_RUNTIME_HANDLES = (ctypes.CDLL(str(tcl), mode=ctypes.RTLD_GLOBAL),
                               ctypes.CDLL(str(tk), mode=ctypes.RTLD_GLOBAL))
        return str(tk.resolve())
    return None


@lru_cache(maxsize=1)
def register_chinese_fonts():
    library = find_library('fontconfig')
    if not library:
        raise RuntimeError('缺少 Fontconfig，无法加载项目中文字体')
    fontconfig = ctypes.CDLL(library)
    fontconfig.FcConfigGetCurrent.restype = ctypes.c_void_p
    fontconfig.FcConfigGetCurrent.argtypes = []
    fontconfig.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    fontconfig.FcConfigAppFontAddFile.restype = ctypes.c_int
    config = fontconfig.FcConfigGetCurrent()
    if not config:
        raise RuntimeError('无法取得当前进程字体配置')
    for weight in ('Regular', 'Bold'):
        path = FONT_DIRECTORY / f'NotoSansCJKsc-{weight}.otf'
        if not path.is_file() or not fontconfig.FcConfigAppFontAddFile(config, str(path).encode()):
            raise RuntimeError(f'中文字体加载失败：{path}')
    return FONT_FAMILY


def configure_tk_fonts(root, size=12):
    from tkinter import font, ttk
    family = register_chinese_fonts()
    available = {name.casefold(): name for name in font.families(root)}
    if family.casefold() in available:
        family = available[family.casefold()]
    else:
        family = next((available[name.casefold()] for name in ('Noto Sans SC', 'Droid Sans Fallback', 'WenQuanYi Micro Hei')
                       if name.casefold() in available), font.nametofont('TkDefaultFont', root=root).actual('family'))
        print(f'界面字体：Tk 未列出项目字体，暂用 {family}；继续启动。', file=sys.stderr)
    for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont',
                 'TkCaptionFont', 'TkSmallCaptionFont', 'TkIconFont', 'TkTooltipFont'):
        font.nametofont(name, root=root).configure(family=family, size=size)
    root.option_add('*Font', (family, size))
    root.option_add('*TCombobox*Listbox.font', (family, size))
    style = ttk.Style(root)
    style.configure('.', font=(family, size))
    style.configure('TButton', padding=(8, 5))
    return family
