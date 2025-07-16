import os
from openai import OpenAI
from fpdf import FPDF
from dotenv import load_dotenv

load_dotenv()  # load .env variables

client = OpenAI()  # relies on OPENAI_API_KEY in env

def transcribe_audio(file_path):
    with open(file_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file
        )
    return transcript.text  # check your response structure if error

def summarize_text(text):
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that summarizes audio transcripts."},
            {"role": "user", "content": f"Summarize this transcript in under 500 words. The first line should be the title.:\n{text}"}
        ],
        temperature=0.5,
        max_tokens=500
    )
    return response.choices[0].message.content.strip()

def generate_pdf(summary, output_path):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    split_summary = summary.split('\n')
    # print(split_summary)  # optional debugging

    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, split_summary[0], ln=True, align="C")

    pdf.set_font("Arial", size=12)
    for line in split_summary[1:]:
        pdf.multi_cell(0, 6, line)
    pdf.output(output_path)

def audio_to_pdf_summary(audio_path, pdf_path):
    transcript = transcribe_audio(audio_path)
    summary = summarize_text(transcript)
    generate_pdf(summary, pdf_path)
    print("PDF Summary saved to:", pdf_path)
