import os
import math
from openai import OpenAI
from fpdf import FPDF
from dotenv import load_dotenv
from pydub import AudioSegment
import tempfile
import tiktoken
import subprocess
import shutil



load_dotenv()
client = OpenAI()

# Target size per chunk (bytes). 24MB leaves buffer under 25MB Whisper limit.
CHUNK_TARGET_SIZE = 24 * 1024 * 1024


def sanitize_for_fpdf(text):
    # Replace em dash and smart quotes with simple equivalents
    replacements = {
        "—": "-",  # em dash
        "–": "-",  # en dash
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "…": "...",
        "•": "-",  # bullets
        " ": " ",  # narrow no-break space
        "−": "-",  # minus sign
        " ": " ",  # non-breaking space
    }
    for k, v in replacements.items():
        text = text.replace(k, v)

    # Ensure it's compatible with FPDF (Latin-1)
    return text.encode("latin-1", errors="replace").decode("latin-1")

def split_audio_by_size(file_path, chunk_target_size=CHUNK_TARGET_SIZE):
    audio = AudioSegment.from_file(file_path)
    total_size = os.path.getsize(file_path)
    duration_ms = len(audio)

    # Estimate bytes per millisecond
    bytes_per_ms = total_size / duration_ms
    chunk_length_ms = math.floor(chunk_target_size / bytes_per_ms)

    chunks = []
    for i in range(0, duration_ms, chunk_length_ms):
        chunk = audio[i:i + chunk_length_ms]

        # Create a temp file name only, don't keep the file handle open
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            chunk_path = tmp.name

        chunk.export(chunk_path, format="mp3")  # now safely write to it
        chunks.append(chunk_path)

    return chunks

def transcribe_audio(file_path, progress_callback=None):
    chunk_paths = split_audio_by_size(file_path)
    full_transcript = ""

    for i, chunk_path in enumerate(chunk_paths):
        if progress_callback:
            progress_callback(f"Transcribing chunk {i + 1} of {len(chunk_paths)}...")
        with open(chunk_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
            full_transcript += transcript.text + "\n\n"
        os.remove(chunk_path)

    if progress_callback:
        progress_callback("Transcription complete.")
    return full_transcript.strip()

def format_transcription(text, progress_callback=None):
    chunks = chunk_text_by_tokens(text)
    formatted_chunks = []


    for i, chunk in enumerate(chunks):
        if progress_callback:
            progress_callback(f"Formatting chunk {i + 1} of {len(chunks)}...")
        response = client.chat.completions.create(
            model="o4-mini-2025-04-16",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that formats audio transcripts."},
                {"role": "user", "content": f"Add punctuation and paragraphing to this transcript:\n{chunk}"}
            ],
            max_completion_tokens=40000
        )
        formatted_chunks.append(response.choices[0].message.content.strip())

    if progress_callback:
        progress_callback("Formatting complete.")

    return "\n\n".join(formatted_chunks)

def summarise_text_from_transcript(text, progress_callback=None):
    def safe_request(prompt, context_name="summary"):
        try:
            response = client.chat.completions.create(
                model="o4-mini-2025-04-16",
                messages=prompt,
                max_completion_tokens=20000
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            if progress_callback:
                progress_callback(f"[ERROR] {context_name} request failed: {str(e)}")
            return None

    # Use smaller chunk size to prevent overflow
    chunks = chunk_text_by_tokens(text, max_tokens=20000)
    partial_summaries = []

    # One-shot summary if small
    if len(chunks) == 1:
        if progress_callback:
            progress_callback("Summarizing transcript...")
        prompt = [
            {"role": "system", "content": "You are a helpful assistant that summarizes transcripts."},
            {"role": "user", "content": f"Please summarize the following transcript into a few paragraphs. The first line should be a title (no more than 9 words):\n\n{chunks[0]}"}
        ]
        summary = safe_request(prompt, "one-shot summary")
        if not summary:
            return "[ERROR] Summary failed."
        if progress_callback:
            progress_callback("Summary complete.")
        return summary

    # Chunked summarization
    for i, chunk in enumerate(chunks):
        if progress_callback:
            progress_callback(f"Summarizing chunk {i + 1} of {len(chunks)}...")
        prompt = [
            {"role": "system", "content": "You are a helpful assistant that summarizes transcripts."},
            {"role": "user", "content": f"Summarize this part of a transcript into a paragraph:\n{chunk}"}
        ]
        summary = safe_request(prompt, f"chunk {i + 1}")
        if summary:
            partial_summaries.append(summary)
        else:
            partial_summaries.append(f"(Chunk {i+1} could not be summarized.)")

    # Final summary step
    if progress_callback:
        progress_callback("Generating final summary...")

    combined = "\n\n".join(partial_summaries)

    # Trim if over safe token limit (leave room for output)
    enc = tiktoken.get_encoding("cl100k_base")
    combined_tokens = enc.encode(combined)
    MAX_FINAL_INPUT_TOKENS = 15000

    if len(combined_tokens) > MAX_FINAL_INPUT_TOKENS:
        if progress_callback:
            progress_callback("Truncating final input to stay within token limits.")
        combined = enc.decode(combined_tokens[:MAX_FINAL_INPUT_TOKENS])

    final_prompt = [
        {"role": "system", "content": "You are a helpful assistant that summarizes summaries."},
        {"role": "user", "content": f"Combine and refine the following summaries into a few concise paragraphs. The first line should be a title (no more than 9 words):\n\n{combined}"}
    ]

    final_summary = safe_request(final_prompt, "final summary")

    if progress_callback:
        progress_callback("Summary complete.")

    return final_summary or "[ERROR] Final summary could not be generated."

def chunk_text_by_tokens(text, max_tokens=20000):
    enc = tiktoken.get_encoding("cl100k_base")
    paragraphs = text.split("\n")

    chunks = []
    current_chunk = []
    current_tokens = 0

    for para in paragraphs:
        token_count = len(enc.encode(para))
        if token_count > max_tokens:
            # Paragraph too large: split by sentence or truncate
            sentences = para.split(". ")
            temp_chunk = []
            temp_tokens = 0
            for sentence in sentences:
                sentence_tokens = len(enc.encode(sentence))
                if temp_tokens + sentence_tokens > max_tokens:
                    chunks.append(". ".join(temp_chunk))
                    temp_chunk = [sentence]
                    temp_tokens = sentence_tokens
                else:
                    temp_chunk.append(sentence)
                    temp_tokens += sentence_tokens
            if temp_chunk:
                chunks.append(". ".join(temp_chunk))
        elif current_tokens + token_count > max_tokens:
            chunks.append("\n".join(current_chunk))
            current_chunk = [para]
            current_tokens = token_count
        else:
            current_chunk.append(para)
            current_tokens += token_count

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks

def generate_pdf_from_text(title, body_lines, output_path, progress_callback=None):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, sanitize_for_fpdf(title), ln=True, align="C")

    pdf.set_font("Arial", size=12)

    if progress_callback:
        progress_callback(f"Writing {len(body_lines)} lines to PDF...")

    for i, line in enumerate(body_lines):
        clean_line = sanitize_for_fpdf(line)
        pdf.multi_cell(0, 6, clean_line)

    pdf.output(output_path)
    if progress_callback:
        progress_callback(f"PDF generation complete: {output_path}")


def generate_latex_from_transcript(transcript_text, output_dir="uploads", tex_filename="transcript_body.tex", progress_callback=None):
    import os

    os.makedirs(output_dir, exist_ok=True)
    tex_path = os.path.join(output_dir, tex_filename)

    if progress_callback:
        progress_callback("Chunking transcript for LaTeX generation...")

    chunks = chunk_text_by_tokens(transcript_text, max_tokens=20000)
    latex_bodies = []

    for i, chunk in enumerate(chunks):
        if progress_callback:
            progress_callback(f"Generating LaTeX for chunk {i+1} of {len(chunks)}...")

        response = client.chat.completions.create(
            model="o4-mini-2025-04-16",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that converts transcripts to LaTeX."},
                {"role": "user", "content": f"Convert this into LaTeX body code with punctuation. Escape all special characters properly. Do NOT include document preamble or \\begin{{document}}:\n\n{chunk}"}
            ],
            max_completion_tokens=40000
        )

        body = response.choices[0].message.content.strip()
        if body.startswith("```"):
            body = "\n".join(body.splitlines()[1:-1])

        if progress_callback:
            progress_callback("Cleaning latex")
        clean_body = clean_latex_unicode(body)
        latex_bodies.append(clean_body)

    final_tex = (
        "\\documentclass{article}\n"
        "\\usepackage[margin=1in]{geometry}\n"
        "\\usepackage{amsmath, amssymb}\n"
        "\\usepackage{enumitem}\n"
        "\\usepackage{url}\n"
        "\\begin{document}\n\n"
        + "\n\n".join(latex_bodies)
        + "\n\n\\end{document}"
    )

    with open(tex_path, "w", encoding="utf-8") as f:
        f.write(final_tex)

    if progress_callback:
        progress_callback(f"LaTeX file written: {tex_path}")

    return tex_path


def clean_latex_unicode(text):
    """
    Replaces problematic Unicode characters in GPT output with LaTeX-safe equivalents.
    Useful for sanitizing text before writing to a .tex file.
    """
    replacements = {
        # Dashes and quotes
        "−": "-",    # minus
        "–": "-",    # en dash
        "—": "--",   # em dash
        "“": "``",   # left double quote
        "”": "''",   # right double quote
        "‘": "`",    # left single quote
        "’": "'",    # right single quote
        "‚": ",",    # single low-9 quotation mark
        "„": ",,",   # double low-9 quotation mark

        # Ellipsis, bullets, degrees, fractions
        "…": "...",
        "•": r"\textbullet{}",
        "°": r"$^\circ$",
        "¼": r"\textonequarter{}",
        "½": r"\textonehalf{}",
        "¾": r"\textthreequarters{}",

        # Currency and symbols
        "€": r"\euro{}",
        "£": r"\pounds{}",
        "©": r"\textcopyright{}",
        "®": r"\textregistered{}",
        "™": r"\texttrademark{}",
        "\u00A0": " ",  # non-breaking space

        # Mathematical operators
        "×": r"\times",
        "÷": r"\div",
        "±": r"\pm",
        "∓": r"\mp",
        "≈": r"\approx",
        "≠": r"\neq",
        "≤": r"\leq",
        "≥": r"\geq",
        "∑": r"\sum",
        "∏": r"\prod",
        "√": r"\sqrt{}",
        "∞": r"\infty",
        "∫": r"\int",
        "∂": r"\partial",
        "∇": r"\nabla",
        "∈": r"\in",
        "∉": r"\notin",
        "∩": r"\cap",
        "∪": r"\cup",
        "⊂": r"\subset",
        "⊃": r"\supset",
        "⊆": r"\subseteq",
        "⊇": r"\supseteq",
        "∧": r"\land",
        "∨": r"\lor",
        "¬": r"\neg",
        "∀": r"\forall",
        "∃": r"\exists",
        "⇒": r"\Rightarrow",
        "⇐": r"\Leftarrow",
        "⇔": r"\Leftrightarrow",
        "→": r"\rightarrow",
        "←": r"\leftarrow",
        "↔": r"\leftrightarrow",

        # Greek lowercase letters
        "α": r"\alpha",
        "β": r"\beta",
        "γ": r"\gamma",
        "δ": r"\delta",
        "ε": r"\epsilon",
        "ζ": r"\zeta",
        "η": r"\eta",
        "θ": r"\theta",
        "ι": r"\iota",
        "κ": r"\kappa",
        "λ": r"\lambda",
        "μ": r"\mu",
        "ν": r"\nu",
        "ξ": r"\xi",
        "ο": "o",
        "π": r"\pi",
        "ρ": r"\rho",
        "σ": r"\sigma",
        "τ": r"\tau",
        "υ": r"\upsilon",
        "φ": r"\phi",
        "χ": r"\chi",
        "ψ": r"\psi",
        "ω": r"\omega",

        # Greek uppercase letters
        "Γ": r"\Gamma",
        "Δ": r"\Delta",
        "Θ": r"\Theta",
        "Λ": r"\Lambda",
        "Ξ": r"\Xi",
        "Π": r"\Pi",
        "Σ": r"\Sigma",
        "Υ": r"\Upsilon",
        "Φ": r"\Phi",
        "Ψ": r"\Psi",
        "Ω": r"\Omega",

        # Greek variant letters
        "ϵ": r"\varepsilon",
        "ϑ": r"\vartheta",
        "ϕ": r"\varphi",
        "ς": r"\varsigma",

        # Accents and diacritics (common examples)
        "á": r"\'{a}",
        "é": r"\'{e}",
        "í": r"\'{i}",
        "ó": r"\'{o}",
        "ú": r"\'{u}",
        "ñ": r"\~{n}",
        "ü": r"\"{u}",
        "ç": r"\c{c}",

        # Miscellaneous symbols
        "¶": r"\P",
        "§": r"\S",
        "†": r"\dagger",
        "‡": r"\ddagger",
        "‰": r"\permil",
        "′": r"'",
        "″": r"''",
        "‴": r"'''",
        "⁄": "/",

        # Arrows (additional)
        "↗": r"\nearrow",
        "↘": r"\searrow",
        "↙": r"\swarrow",
        "↖": r"\nwarrow",
        "⇑": r"\Uparrow",
        "⇓": r"\Downarrow",

        # Superscripts and subscripts
        "¹": r"^{1}",
        "²": r"^{2}",
        "³": r"^{3}",

        # Other symbols
        "↦": r"\mapsto",
        "∘": r"\circ",
        "∙": r"\cdot",
        "†": r"\dag",

        # Thin spaces and spacing (optional, but often useful)
        "\u2009": r"\,",      # thin space
        "\u2002": r"\enspace",# en space
        "\u2003": r"\quad",   # em space
        "\u2011": "-",        # non-breaking hyphen
    }

    for bad_char, replacement in replacements.items():
        text = text.replace(bad_char, replacement)

    return text


def compile_latex_to_pdf(latex_file, pdf_path, progress_callback=None):
    """
    Compiles a .tex file into a PDF using pdflatex.
    Parameters:
        latex_file: str - path to the .tex file
        pdf_path: str - desired path for the output PDF
        progress_callback: function(str) - optional progress updates
    Returns:
        path to generated PDF (pdf_path) if success, else None
    """

    output_dir = os.path.dirname(latex_file)
    tex_filename = os.path.basename(latex_file)

    if progress_callback:
        progress_callback(f"Compiling {tex_filename} to PDF...")

    try:
        # Run pdflatex, suppress output
        subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", tex_filename],
            cwd=output_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True
        )

        generated_pdf = os.path.join(output_dir, tex_filename.replace(".tex", ".pdf"))

        if not os.path.exists(generated_pdf):
            if progress_callback:
                progress_callback("PDF was not generated by pdflatex.")

        # If desired pdf_path is different, move the file
        if os.path.abspath(generated_pdf) != os.path.abspath(pdf_path):
            shutil.move(generated_pdf, pdf_path)

        if progress_callback:
            progress_callback(f"PDF successfully generated at {pdf_path}")



    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if progress_callback:
            progress_callback(f"LaTeX compilation failed: {e}")


def generate_summary_from_base_transcript(transcript, pdf_path, progress_callback=None):
    summary = summarise_text_from_transcript(transcript, progress_callback)

    if not summary or summary.startswith("[ERROR]"):
        print("[ERROR] Summary generation failed. Content was:", summary)
        summary = "Summary Unavailable\nCould not generate summary from transcript."

    # Safely split into title and body
    lines = sanitize_for_fpdf(summary).split("\n") # change bad characters

    title = lines[0] if lines else "Summary Unavailable"
    body = lines[1:] if len(lines) > 1 else ["No content available."]

    generate_pdf_from_text(title, body, pdf_path, progress_callback)



def generate_formatted_transcript_from_base_transcript(transcript, pdf_path, progress_callback=None):
    formatted = format_transcription(transcript, progress_callback)
    lines = sanitize_for_fpdf(formatted).split("\n")
    generate_pdf_from_text("Transcription", lines, pdf_path, progress_callback)



def generate_math_pdf_from_transcipt(transcript, pdf_path, progress_callback = None):
    latex_file = generate_latex_from_transcript(transcript, "uploads", "Math_Transcription.tex", progress_callback)



    compile_latex_to_pdf(latex_file, pdf_path, progress_callback)

