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
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        chunk.export(temp_file.name, format="mp3")
        chunks.append(temp_file.name)

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
            model="gpt-3.5-turbo-16k",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that formats audio transcripts."},
                {"role": "user", "content": f"Add punctuation and paragraphing to this transcript:\n{chunk}"}
            ],
            temperature=0.5,
            max_tokens=4000
        )
        formatted_chunks.append(response.choices[0].message.content.strip())

    if progress_callback:
        progress_callback("Formatting complete.")

    return "\n\n".join(formatted_chunks)

def summarise_text_from_transcipt(text, progress_callback=None):
    chunks = chunk_text_by_tokens(text)
    partial_summaries = []

    for i, chunk in enumerate(chunks):
        if progress_callback:
            progress_callback(f"Summarizing chunk {i + 1} of {len(chunks)}...")
        response = client.chat.completions.create(
            model="gpt-3.5-turbo-16k",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that summarizes transcripts."},
                {"role": "user", "content": f"Summarize this part of a transcript into bullet points:\n{chunk}"}
            ],
            temperature=0.5,
            max_tokens=800
        )
        partial_summaries.append(response.choices[0].message.content.strip())

    # Final summarization step
    combined = "\n\n".join(partial_summaries)
    if progress_callback:
        progress_callback("Generating final summary...")

    final_response = client.chat.completions.create(
        model="gpt-3.5-turbo-16k",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that summarizes summaries."},
            {"role": "user", "content": f"Combine and refine this summary into a single piece of bullet points. The first line should be a title of no more than 9 words.:\n{combined}"}
        ],
        temperature=0.5,
        max_tokens=800
    )

    if progress_callback:
        progress_callback("Summary complete.")

    return final_response.choices[0].message.content.strip()

def chunk_text_by_tokens(text, model="gpt-3.5-turbo-16k", max_tokens=3500):
    enc = tiktoken.encoding_for_model(model)
    paragraphs = text.split("\n")

    chunks = []
    current_chunk = []
    current_tokens = 0

    for para in paragraphs:
        token_count = len(enc.encode(para))
        if current_tokens + token_count > max_tokens:
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
    pdf.cell(0, 10, title, ln=True, align="C")

    pdf.set_font("Arial", size=12)

    if progress_callback:
        progress_callback(f"Writing {len(body_lines)} lines to PDF...")

    for i, line in enumerate(body_lines):
        pdf.multi_cell(0, 6, line)

    pdf.output(output_path)
    if progress_callback:
        progress_callback(f"PDF generation complete: {output_path}")


def generate_latex_from_transcript(
    transcript_text,
    output_dir="uploads",
    tex_filename="transcript_body.tex",
    progress_callback=None
):
    os.makedirs(output_dir, exist_ok=True)
    tex_path = os.path.join(output_dir, tex_filename)

    if progress_callback:
        progress_callback("Generating LaTeX body content from transcript...")

    # Request only body content (no preamble)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an assistant that punctuates and converts transcript text into a full LaTeX document. "
                    "The LaTeX document must include the documentclass declaration, necessary packages "
                    "(such as amsmath and geometry), and begin/end document commands. "
                    "Respond only with the full LaTeX code, and nothing else — no explanations or text before or after."
                )
            },
            {
                "role": "user",
                "content": (
                        "Convert the following transcript into a complete LaTeX document with punctuation with a preamble. "
                        "Do not include any explanations — output only the raw LaTeX code:\n\n"
                        + transcript_text
                )
            }
        ],
        temperature=0.3,
        max_tokens=4000
    )


    latex_raw = response.choices[0].message.content.strip()

    # Remove Markdown-style code fences if present
    if latex_raw.startswith("```") and latex_raw.endswith("```"):
        latex_lines = latex_raw.splitlines()
        # Strip the first and last lines (```latex and ```)
        latex_clean = "\n".join(latex_lines[1:-1])
    else:
        latex_clean = latex_raw

    with open(tex_path, "w", encoding="utf-8") as f:
        f.write(latex_clean)

    if progress_callback:
        progress_callback(f"LaTeX body written to {tex_path}")

    return tex_path

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
    summary = summarise_text_from_transcipt(transcript, progress_callback)
    lines = summary.replace("—", "-").encode("latin-1", errors="replace").decode("latin-1").split("\n")
    generate_pdf_from_text(lines[0], lines[1:], pdf_path, progress_callback)

def generate_formatted_transcript_from_base_transcript(transcript, pdf_path, progress_callback=None):
    formatted = format_transcription(transcript, progress_callback)
    lines = formatted.replace("—", "-").encode("latin-1", errors="replace").decode("latin-1").split("\n")
    generate_pdf_from_text("Transcription", lines, pdf_path, progress_callback)

def generate_math_pdf_from_transcipt(transcript, pdf_path, progress_callback = None):
    latex_file = generate_latex_from_transcript(transcript, "uploads", "Math_Transcription.tex", progress_callback)
    compile_latex_to_pdf(latex_file, pdf_path, progress_callback)