import os
import math
from openai import OpenAI
from fpdf import FPDF
from dotenv import load_dotenv
from pydub import AudioSegment
import tempfile





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

def transcribe_audio(file_path):
    chunk_paths = split_audio_by_size(file_path)
    full_transcript = ""

    for i, chunk_path in enumerate(chunk_paths):
        with open(chunk_path, "rb") as audio_file:
            print(f"Transcribing chunk {i + 1}/{len(chunk_paths)}: {chunk_path}")
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
            full_transcript += transcript.text + "\n\n"
        os.remove(chunk_path)  # cleanup

    return full_transcript.strip()

def format_transcription(text):
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that formats audio transcripts."},
            {"role": "user", "content": f"Add punctuation and paragraphing to this transcript:\n{text}"}
        ],
        temperature=0.5,
        max_tokens=4000
    )
    return response.choices[0].message.content.strip()

def summarize_text(text):
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that summarizes audio transcripts."},
            {"role": "user", "content": f"Summarize this transcript in under 500 words. Begin with a title:\n{text}"}
        ],
        temperature=0.5,
        max_tokens=800
    )
    return response.choices[0].message.content.strip()

def generate_pdf(title, body_lines, output_path):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, title, ln=True, align="C")

    pdf.set_font("Arial", size=12)
    for line in body_lines:
        pdf.multi_cell(0, 6, line)

    pdf.output(output_path)

def audio_to_pdf_summary(transcript, pdf_path):
    summary = summarize_text(transcript)
    lines = summary.replace("—", "-").encode("latin-1", errors="replace").decode("latin-1").split("\n")
    generate_pdf(lines[0], lines[1:], pdf_path)

def audio_to_pdf_transcript(transcript, pdf_path):
    formatted = format_transcription(transcript)
    lines = formatted.replace("—", "-").encode("latin-1", errors="replace").decode("latin-1").split("\n")
    generate_pdf("Transcription", lines, pdf_path)
