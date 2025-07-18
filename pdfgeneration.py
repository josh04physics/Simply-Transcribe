import os
import math
from openai import OpenAI
from fpdf import FPDF
from dotenv import load_dotenv
from pydub import AudioSegment
import tempfile
import tiktoken

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

def summarize_text(text, progress_callback=None):
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

def generate_pdf(title, body_lines, output_path, progress_callback=None):
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

def audio_to_pdf_summary(transcript, pdf_path, progress_callback=None):
    summary = summarize_text(transcript, progress_callback)
    lines = summary.replace("—", "-").encode("latin-1", errors="replace").decode("latin-1").split("\n")
    generate_pdf(lines[0], lines[1:], pdf_path, progress_callback)

def audio_to_pdf_transcript(transcript, pdf_path, progress_callback=None):
    formatted = format_transcription(transcript, progress_callback)
    lines = formatted.replace("—", "-").encode("latin-1", errors="replace").decode("latin-1").split("\n")
    generate_pdf("Transcription", lines, pdf_path, progress_callback)
