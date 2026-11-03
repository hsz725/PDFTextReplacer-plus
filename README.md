# PDF Text Replacer Plus

[English](README.md) | [简体中文](README.zh-CN.md)

---

A Windows desktop tool for editing PDF content, built with Python, Tkinter, and PyMuPDF.
Based on [ShawnJim/PDFTextReplacer](https://github.com/ShawnJim/PDFTextReplacer) (MIT),
extended with region erasing, open-license CJK fonts, and a light build.

## 📦 Two Editions

Both editions are built from the **same source code** — the light edition simply hides
the erase tab. They are almost identical in size, so pick by workflow, not by size.

| Edition | Download | Local build | Features |
|---|---|---|---|
| **Full** | `PDFToolbox.exe` | `PDF工具箱.exe` | Text replacement **+** region erasing |
| **Light** | `PDFToolbox-Lite.exe` | `PDF文本替换工具.exe` | Text replacement only, single-screen UI |

> Download names are ASCII because GitHub drops non-ASCII characters from release
> asset names — two Chinese-named files would collide into one. The app itself,
> its UI and docs are all in Chinese.

Download either one from the [Releases](../../releases) page. Both are single-file
executables — no installation, no extra folders.

## ✨ Key Features

### Text replacement

- **Intuitive GUI**: A simple graphical interface for all operations, no command line needed.
- **Dynamic Rule Editor**: Add, edit, or paste replacement rules (`old_text|new_text`) directly within the application.
- **Multiple Replacement Strategies**:
  - **`precise`**: (Default) Accurately finds and replaces text, preserving original fonts, sizes, and colors.
  - **`overlay`**: Covers the original text with a white rectangle before writing the new text, ensuring maximum compatibility.
  - **`hybrid`**: Automatically selects the best strategy for each replacement.
- **Open-license CJK fonts**: Ships with Noto Sans SC / Noto Serif SC (SIL OFL 1.1) and
  matches the original font style by the font's **internal family name**
  (SimSun → Noto Serif, SimHei → Noto Sans).
- **Font scope switch**: *Open-source only* (default, output is safe to redistribute) or
  *All fonts* (also reads Windows system fonts — personal use only).
- **Custom font file**: point to any `.ttf`/`.otf`/`.ttc` without copying it;
  it takes priority over everything else.
- **Live Logging** and **Result Verification**.

### Region erasing (full edition only)

- Erase **everything** inside a rectangle: text, images, vector graphics — not just text.
- Rectangle defined by top-left corner + width/height, in `pt` or `mm`.
- Background color is sampled from the page, so the erased area blends in.
- **Preview** the rectangle on the page before applying.

> Coordinates use a **top-left origin** (X right, Y down), matching Acrobat / Foxit
> measurement tools. The PDF file format's bottom-left origin is handled internally.


## 🚀 How to Use (For End-Users)

1.  Download either `PDF工具箱.exe` (full) or `PDF文本替换工具.exe` (light) from the [Releases](../../releases) page.
2.  **Select Input PDF**: Click "Browse..." to choose the PDF file you want to modify.
3.  **Specify Output PDF**: An output path with an `_replaced` suffix is automatically generated. You can also click "Select..." to customize the destination.
4.  **Enter Replacement Rules**: In the "Replacement Rules" text box, enter your rules using the `old_text|new_text` format, one rule per line. For example:
    ```
    Old Company Name|New Company Name
    Manager A|Manager B
    ```
5.  **Choose Method**: Select a replacement method from the dropdown menu (`precise` is recommended).
6.  **Start Processing**: Click the "Start Processing" button. You can monitor the progress in the log view below.
7.  **Done**: A confirmation message will appear upon completion. Your modified PDF will be at the specified output location.

## 🛠️ How to Build (For Developers)

If you want to run from source or rebuild the application, follow these steps.

### 1. Prerequisites

- Python 3.8+ installed.
- Git installed.

### 2. Clone & Setup Environment

```bash
# Clone the repository
git clone https://github.com/hsz725/PDFTextReplacer-plus.git
cd PDFTextReplacer-plus

# Create and activate a virtual environment (recommended)
python -m venv .venv
# On Windows
.venv\Scripts\activate
# On macOS/Linux
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
pip install pyinstaller
```

### 4. Run from Source

```bash
python gui.py
```

### 5. Package the executables

One script builds both editions:

```bash
python tools/build.py          # both
python tools/build.py --lite   # light only
python tools/build.py --full   # full only
```

It stages only the distributable fonts (skipping `fonts/_src`, which holds the 40MB+
variable font sources) and excludes PyMuPDF's layout/OCR dependencies, keeping each
exe at ~43MB instead of ~66MB. Output goes to the repository root.

### 6. Verify a build

```bash
"PDF工具箱.exe" --selftest
```

Runs the replacement and erase pipelines without opening the GUI, and writes the result
to `%TEMP%\pdfbox_selftest.log` (windowed executables have no console). Exit code 0 = pass.

## Command-Line Usage (Advanced)

For automation or batch processing, you can use the core script directly from the command line. This requires a separate text file for rules.

**Usage:**
```bash
python pdf_replacer_pymupdf.py <input_pdf> <output_pdf> <rules_file> [--method <method>] [--verify]
```

**Example:**
```bash
# Use the default 'precise' method and verify the result
python pdf_replacer_pymupdf.py document.pdf document_updated.pdf rules.txt --verify
```

## 📄 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

Bundled fonts (Noto Sans SC, Noto Serif SC) are licensed under the SIL Open Font
License 1.1 — see [fonts/OFL.txt](fonts/OFL.txt).

Based on [ShawnJim/PDFTextReplacer](https://github.com/ShawnJim/PDFTextReplacer).