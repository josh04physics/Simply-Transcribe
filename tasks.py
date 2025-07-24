import os
from flask import current_app
import io
import zipfile
from queue import Queue
from utils import progress_queues, results_cache
from pdfgeneration import (
    generate_pdf_from_text,
    generate_word_doc_from_text,
    generate_latex_pdf_from_transcipt,
    format_transcription,
    summarise_text_from_transcript,
    transcribe_audio
)


def background_process_file(app, audio_path, filename, outputs):
    # Get existing queue or create new
    q = progress_queues.setdefault(filename, Queue())

    def log(msg):
        q.put(msg)

    try:
        with app.app_context():
            log("Transcribing audio...")
            transcript = transcribe_audio(audio_path)

            formatted_transcript = None
            summary = None

            if 'transcript' in outputs or 'latex' in outputs:
                log("Generating formatted transcript...")
                formatted_transcript = format_transcription(transcript)

            if 'summary' in outputs:
                log("Generating summary...")
                summary = summarise_text_from_transcript(transcript)

            log("Storing results...")
            # Store result in memory so check_results can return it
            results_cache[filename] = {
                "transcript": formatted_transcript,
                "summary": summary,
                "outputs": outputs
            }
    except Exception as e:
        log(f"❌ Error during processing: {str(e)}")
    finally:
        # Signal to frontend that processing is done
        log("[DONE]")


def background_generate_outputs(app, transcript, summary, filename, outputs):
    q = progress_queues.setdefault(filename, Queue())

    def log(msg):
        q.put(msg)

    try:
        with app.app_context():
            output_files = []

            if transcript:
                if 'transcript' in outputs:
                    log("Generating final transcript PDF...")
                    paragraphs = [p.strip() for p in transcript.split("\n") if p.strip()]
                    pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{filename}-edited-transcript.pdf")
                    docx_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{filename}-edited-transcript.docx")
                    generate_pdf_from_text("Transcript", paragraphs, pdf_path)
                    generate_word_doc_from_text("Transcript", paragraphs, docx_path)
                    output_files.append((pdf_path, "edited-transcript.pdf"))
                    output_files.append((docx_path, "edited-transcript.docx"))
                    log("Final Transcript PDF Generated")

                if 'latex' in outputs:
                    log("Generating LaTeX file...")
                    latex_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{filename}-edited-transcript-latex.pdf")
                    log("LaTeX file generated")
                    log("Generating PDF from LaTeX...")
                    generate_latex_pdf_from_transcipt(transcript, latex_path)
                    output_files.append((latex_path, "edited-transcript-latex.pdf"))
                    tex_path = os.path.join(app.config['UPLOAD_FOLDER'], f"Math_Transcription.tex")
                    output_files.append((tex_path, "edited-transcript-latex.tex"))
                    log("LaTeX PDF Generated")

            if summary:
                log("Generating final summary PDF...")
                summary_paragraphs = [p.strip() for p in summary.split("\n") if p.strip()]
                pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{filename}-edited-summary.pdf")
                docx_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{filename}-edited-summary.docx")
                generate_pdf_from_text("Summary", summary_paragraphs, pdf_path)
                generate_word_doc_from_text("Summary", summary_paragraphs, docx_path)
                output_files.append((pdf_path, "edited-summary.pdf"))
                output_files.append((docx_path, "edited-summary.docx"))
                log("Final summary PDF generated")

            # Create ZIP in memory
            log("Nearly done! Generating ZIP file...")
            memory_file = io.BytesIO()
            with zipfile.ZipFile(memory_file, 'w') as zf:
                for path, arcname in output_files:
                    if os.path.exists(path):
                        zf.write(path, arcname=arcname)
                    else:
                        print(f"Warning: file {path} does not exist!")
            memory_file.seek(0)

            # Update results_cache entry, preserving existing data if present
            existing = results_cache.get(filename, {})
            existing.update({
                "zip_ready": True,
                "zip_data": memory_file
            })
            results_cache[filename] = existing

            log("ZIP file for outputs generated and cached.")
    except Exception as e:
        log(f"❌ Error during output generation: {str(e)}")
    finally:
        # Signal to frontend that output generation is done
        log("[DONE]")
