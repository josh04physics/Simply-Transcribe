from flask import Flask, request, render_template, send_file
import os
from pdfgeneration import audio_to_pdf_summary, audio_to_pdf_transcript, transcribe_audio
import zipfile
import io

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


@app.route('/')
def index():
    return render_template('upload.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'audio_file' not in request.files:
        return "No file part", 400

    file = request.files['audio_file']
    if file.filename == '':
        return "No selected file", 400

    audio_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
    file.save(audio_path)

    pdf_filename = os.path.splitext(file.filename)[0] + '.pdf'
    transcript_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{pdf_filename}-transcript")
    summary_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{pdf_filename}-summary")

    # Call your actual audio-to-PDF function here
    transcript = transcribe_audio(audio_path) # Done here so that transcription done only once
    audio_to_pdf_transcript(transcript, transcript_path)
    audio_to_pdf_summary(transcript, summary_path)

    # Create an in-memory ZIP file
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w') as zf:
        zf.write(transcript_path, arcname='transcript.pdf')
        zf.write(summary_path, arcname='summary.pdf')
    memory_file.seek(0)

    return send_file(memory_file, as_attachment=True, download_name='audio_outputs.zip')

if __name__ == '__main__':
    app.run(debug=True)