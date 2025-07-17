from flask import Flask, request, render_template, send_file, url_for, redirect, flash, request
from flask_login import login_user, login_required, logout_user, LoginManager, current_user
from flask_migrate import Migrate
import os
from pdfgeneration import audio_to_pdf_summary, audio_to_pdf_transcript, transcribe_audio
import zipfile
import io
from models import db
from models.user import User
from forms.forms import RegisterForm, LoginForm
from flask_bcrypt import Bcrypt

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SECRET_KEY'] = 'your_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
db.init_app(app)

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

bcrypt = Bcrypt(app)

migrate = Migrate(app, db) # To allow columns to be added using terminal

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
    print(db)
    app.run(debug=True)

