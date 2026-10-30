"""Post-process a pptxgenjs file: remove the mid-paragraph <a:pPr> elements pptxgenjs emits for the
second and later runs of a bulleted paragraph (invalid OOXML; LibreOffice and PowerPoint drop the bullet).
Usage: python poster/fix_pptx.py poster/sensei_gtc2027_poster.pptx
"""
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

MID_PPR = re.compile(r"(?<=</a:r>)<a:pPr[^>]*>(?:<a:buNone/>)?</a:pPr>")


def main(path: str) -> None:
    src = Path(path)
    tmp = Path(tempfile.mkstemp(suffix=".pptx")[1])
    removed = 0
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.startswith("ppt/slides/slide") and item.filename.endswith(".xml"):
                text = data.decode("utf-8")
                text, n = MID_PPR.subn("", text)
                removed += n
                data = text.encode("utf-8")
            zout.writestr(item, data)
    shutil.move(tmp, src)
    print(f"{src.name}: removed {removed} mid-paragraph pPr elements")


if __name__ == "__main__":
    main(sys.argv[1])
