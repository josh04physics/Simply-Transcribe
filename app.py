from flask import Flask, request, render_template, send_file, url_for, redirect, flash, Response, stream_with_context
import time
import queue
from flask_login import login_user, login_required, logout_user, LoginManager, current_user
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
import os
from pdfgeneration import audio_to_pdf_summary, audio_to_pdf_transcript, transcribe_audio
import zipfile
import io
from models import db
from models.user import User
from forms.forms import RegisterForm, LoginForm
from flask_bcrypt import Bcrypt
import sys
from pydub.utils import which
from pydub import AudioSegment
from dotenv import load_dotenv


load_dotenv()

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SECRET_KEY'] = 'your_secret_key'

db.init_app(app)

# 1. Set DATABASE_URL from environment or default to local instance folder
database_url = os.environ.get("DATABASE_URL")

if not database_url:
    os.makedirs(app.instance_path, exist_ok=True)
    db_path = os.path.join(app.instance_path, 'users.db')
    database_url = f"sqlite:///{db_path}"
else:
    # Extract file path from DATABASE_URL
    db_path = database_url.replace("sqlite:///", "")

# 2. Configure SQLAlchemy
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# 3. Create DB if it doesn't exist
if not os.path.exists(db_path):
    with app.app_context():
        db.create_all()
        print("✅ Created new database at", db_path)




progress_queue = queue.Queue() # for real time updates on pdf generation


def progress_callback(message):
    progress_queue.put(message)



def set_ffmpeg_path():
    # Detect OS platform
    if sys.platform == "win32":
        # Windows: expect ffmpeg.exe in bin folder
        ffmpeg_path = os.path.join(os.path.dirname(__file__), "bin", "ffmpeg.exe")
        if not os.path.isfile(ffmpeg_path):
            # fallback to system ffmpeg if local binary missing
            ffmpeg_path = which("ffmpeg")
    else:
        # Linux/Mac: expect ffmpeg static binary in bin folder
        ffmpeg_path = os.path.join(os.path.dirname(__file__), "bin", "ffmpeg")
        if not os.path.isfile(ffmpeg_path):
            # fallback to system ffmpeg if local binary missing
            ffmpeg_path = which("ffmpeg")

    if ffmpeg_path is None:
        raise RuntimeError("FFmpeg binary not found! Please provide ffmpeg in bin/ or install globally.")

    AudioSegment.converter = ffmpeg_path

# Call this early in your app startup
set_ffmpeg_path()



os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

bcrypt = Bcrypt(app)

migrate = Migrate(app, db) # To allow columns to be added using terminal




def stream_progress():
    while True:
        message = progress_queue.get()
        yield f"data: {message}\n\n"
        if message == "[DONE]":
            break

@app.route("/progress")
def progress():
    def generate():
        while True:
            message = progress_queue.get()
            yield f"data: {message}\n\n"
            if message == "[DONE]":
                break
    return Response(stream_with_context(generate()), content_type='text/event-stream')



@app.route('/register', methods=['GET', 'POST'])
def register():
    form = RegisterForm()
    if form.validate_on_submit():
        hashed_pw = bcrypt.generate_password_hash(form.password.data).decode('utf-8')
        new_user = User(username=form.username.data, email=form.email.data, password=hashed_pw)
        db.session.add(new_user)
        db.session.commit()
        flash('Account created!', 'success')
        return redirect(url_for('login'))
    return render_template('register.html', form=form)

@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and bcrypt.check_password_hash(user.password, form.password.data):
            login_user(user)
            return redirect(url_for('index'))
        flash('Login failed', 'danger')
    return render_template('login.html', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/')
@login_required
def index(): # MAIN HOMEPAGE
    if current_user.is_authenticated:
        username = current_user.username
        credits = current_user.credits
    else:
        username = None
        credits= None
    return render_template('upload.html', username=username, credits=credits)

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if 'audio_file' not in request.files:
        flash("No file part", "danger")
        return redirect(url_for('index'))

    file = request.files['audio_file']
    if file.filename == '':
        flash("No selected file", "danger")
        return redirect(url_for('index'))

    audio_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
    file.save(audio_path)

    # --- STEP 1: Calculate duration in minutes using pydub ---
    try:
        audio = AudioSegment.from_file(audio_path)
        duration_minutes = max(1, -(-len(audio) // 60000))  # ceil in ms
    except Exception as e:
        flash(f"Failed to read audio: {str(e)}", "danger")
        return redirect(url_for('index'))

    # --- STEP 2: Check if user has enough credits ---
    if current_user.credits < duration_minutes:
        flash(f"You need {duration_minutes} credits, but you only have {current_user.credits}.", "warning")
        return redirect(url_for('index'))

    # --- STEP 3: Deduct credits ---
    current_user.credits -= duration_minutes
    db.session.commit()

    # --- STEP 4: Proceed with transcription ---
    pdf_filename = os.path.splitext(file.filename)[0] + '.pdf'
    transcript_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{pdf_filename}-transcript")
    summary_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{pdf_filename}-summary")

    transcript = transcribe_audio(audio_path, progress_callback)
    audio_to_pdf_transcript(transcript, transcript_path, progress_callback)
    audio_to_pdf_summary(transcript, summary_path, progress_callback)
    progress_callback("[DONE]")

    # Create ZIP for download
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w') as zf:
        zf.write(transcript_path, arcname='transcript.pdf')
        zf.write(summary_path, arcname='summary.pdf')
    memory_file.seek(0)

    return send_file(memory_file, as_attachment=True, download_name='audio_outputs.zip')


if __name__ == '__main__':
    print(db)
    app.run(debug=True)

