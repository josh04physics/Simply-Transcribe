import os
import io
import zipfile
from pdfgeneration import (
    generate_pdf_from_text,
    generate_word_doc_from_text,
    generate_latex_pdf_from_transcipt,
    generate_latex_pdf_from_summary,
    format_transcription,
    summarise_text_from_transcript,
    transcribe_audio
)
from models.progress import Progress
from models.results import Results
from models import db

import yt_dlp
import uuid

def log_progress(filename, message, is_done=False, phase="phase1"):
    progress = Progress(filename=filename, message=message, is_done=is_done, phase=phase)
    db.session.add(progress)
    db.session.commit()

def background_process_file(app, audio_path, filename, outputs):
    try:
        with app.app_context():
            log_progress(filename, "Transcribing audio...", phase="phase1")
            transcript = transcribe_audio(audio_path)

            formatted_transcript = None
            summary = None

            if 'transcript' in outputs or 'latex_transcript' in outputs:
                log_progress(filename, "Generating formatted transcript...", phase="phase1")
                formatted_transcript = format_transcription(transcript)
                print("Formatted transcript generated successfully.")
            if 'summary' in outputs or 'latex_summary' in outputs:
                log_progress(filename, "Generating summary...", phase="phase1")
                summary = summarise_text_from_transcript(transcript)
                print("Summary generated successfully.")

            log_progress(filename, "Storing results...", phase="phase1")
            result = Results(
                filename=filename,
                transcript=formatted_transcript,
                summary=summary,
                outputs=outputs,
                zip_ready=False
            )
            db.session.add(result)
            db.session.commit()


            log_progress(filename, "[DONE]", is_done=True, phase="phase1")
    except Exception as e:
        print(f"Error processing file {filename}: {e}")

# Python
def background_generate_outputs(app, transcript, summary, filename, outputs):
    try:
        with app.app_context():
            log_progress(filename, "Starting output generation...", phase="phase2")

            output_files = []

            if transcript:
                if 'transcript' in outputs:
                    log_progress(filename, "Generating transcript PDFs and DOCX...", phase="phase2")
                    paragraphs = [p.strip() for p in transcript.split("\n") if p.strip()]
                    pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{filename}-edited-transcript.pdf")
                    docx_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{filename}-edited-transcript.docx")
                    generate_pdf_from_text("Transcript", paragraphs, pdf_path)
                    generate_word_doc_from_text("Transcript", paragraphs, docx_path)
                    output_files.append((pdf_path, "edited-transcript.pdf"))
                    output_files.append((docx_path, "edited-transcript.docx"))
                    print("Transcript generated successfully.")

                if 'latex_transcript' in outputs:
                    log_progress(filename, "Generating LaTeX PDF...", phase="phase2")
                    latex_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{filename}-edited-transcript-latex.pdf")
                    generate_latex_pdf_from_transcipt(transcript, latex_path)
                    output_files.append((latex_path, "edited-transcript-latex.pdf"))
                    tex_path = os.path.join(app.config['UPLOAD_FOLDER'], f"Math_Transcription.tex")
                    output_files.append((tex_path, "edited-transcript-latex.tex"))
                    print("Latex PDF generated successfully.")

            if summary:

                if 'summary' in outputs:
                    log_progress(filename, "Generating summary PDFs and DOCX...", phase="phase2")
                    summary_paragraphs = [p.strip() for p in summary.split("\n") if p.strip()]
                    pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{filename}-edited-summary.pdf")
                    docx_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{filename}-edited-summary.docx")
                    generate_pdf_from_text("Summary", summary_paragraphs, pdf_path)
                    generate_word_doc_from_text("Summary", summary_paragraphs, docx_path)
                    output_files.append((pdf_path, "edited-summary.pdf"))
                    output_files.append((docx_path, "edited-summary.docx"))
                    print("Summary generated successfully.")
                if 'latex_summary' in outputs:
                    log_progress(filename, "Generating LaTeX summary PDF...", phase="phase2")
                    latex_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{filename}-edited-summary-latex.pdf")
                    generate_latex_pdf_from_summary(summary, latex_path)
                    output_files.append((latex_path, "edited-summary-latex.pdf"))
                    tex_path = os.path.join(app.config['UPLOAD_FOLDER'], f"Math_Summary.tex")
                    output_files.append((tex_path, "edited-summary-latex.tex"))
                    print("Latex summary PDF generated successfully.")




            log_progress(filename, "Creating ZIP file...", phase="phase2")
            # Create ZIP in memory
            memory_file = io.BytesIO()
            with zipfile.ZipFile(memory_file, 'w') as zf:
                for path, arcname in output_files:
                    if os.path.exists(path):
                        zf.write(path, arcname=arcname)
                    else:
                        print(f"Warning: file {path} does not exist!")
            memory_file.seek(0)

            # Update the database entry for the result
            log_progress(filename, "Updating database with ZIP file...", phase="phase2")
            result = Results.query.filter_by(filename=filename).first()
            if result:
                result.zip_ready = True
                result.zip_data = memory_file.getvalue()  # Store ZIP data as binary
                db.session.commit()
                log_progress(filename, "[DONE]", is_done=True, phase="phase2")  # Mark progress as done
    except Exception as e:
        log_progress(filename, f"❌ Error during output generation: {str(e)}", is_done=True)

def download_youtube_audio(youtube_url, upload_folder):
    try:
        temp_id = str(uuid.uuid4())
        base_path = os.path.join(upload_folder, temp_id)  # no extension here

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': base_path,  # no .mp3 extension here
            'quiet': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])

        # The postprocessor will save file as base_path + ".mp3"
        final_path = base_path + ".mp3"

        if os.path.exists(final_path):
            return final_path
        else:
            print(f"❌ Downloaded file not found: {final_path}")
            return None

    except Exception as e:
        print(f"❌ Error downloading YouTube audio: {e}")
        return None