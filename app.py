from flask import Flask, request, render_template, send_file, url_for, redirect, flash, Response, stream_with_context, jsonify
import time
import queue
from flask_login import login_user, login_required, logout_user, LoginManager, current_user
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
import os
from pdfgeneration import generate_summary_from_base_transcript, generate_formatted_transcript_from_base_transcript, transcribe_audio, generate_math_pdf_from_transcipt
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
import stripe


load_dotenv()


app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SECRET_KEY'] = 'your_secret_key'

stripe.api_key = os.getenv("STRIPE_SECRET_KEY") # get stripe data (different locally and on render)
YOUR_DOMAIN = os.getenv("YOUR_DOMAIN")  # e.g. https://yourdomai


# 1. Set DATABASE_URL from environment or default to local instance folder RIGHT NOW IT DEFAULTS - FIX THIS or does it??? TEMP STOP TO GET BUILD WORKING
#database_url = os.environ.get("DATABASE_URL")


database_url = None
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

db.init_app(app) # order matters of this, must be called after config but before creation.

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



@app.route("/progress")
def progress():
    def generate():
        while True:
            message = progress_queue.get()
            yield f"data: {message}\n\n"
            if message == "[DONE]":
                break
    return Response(stream_with_context(generate()), content_type='text/event-stream')

@app.route('/buy-credits', methods=['GET', 'POST'])
@login_required
def buy_credits():
    MIN_CREDITS = 20  # Minimum credits required to purchase

    if request.method == 'POST':
        try:
            credits_to_buy = int(request.form.get('credits'))
            if credits_to_buy < MIN_CREDITS:
                flash(f'You must purchase at least {MIN_CREDITS} credits.', 'warning')
                return redirect(url_for('buy_credits'))
        except (TypeError, ValueError):
            flash('Invalid input for credits.', 'warning')
            return redirect(url_for('buy_credits'))

        price_per_credit_cents = 2
        amount_to_charge = credits_to_buy * price_per_credit_cents

        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'{credits_to_buy} Credits',
                    },
                    'unit_amount': price_per_credit_cents,
                },
                'quantity': credits_to_buy,
            }],
            mode='payment',
            success_url=YOUR_DOMAIN + url_for('payment_success', _external=False) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=YOUR_DOMAIN + url_for('buy_credits', _external=False),
            client_reference_id=current_user.id,
        )

        return redirect(session.url, code=303)

    return render_template('buy_credits.html')


@app.route('/payment-success')
@login_required
def payment_success():
    session_id = request.args.get('session_id')
    if not session_id:
        flash('Missing payment session ID.', 'danger')
        return redirect(url_for('buy_credits'))

    session = stripe.checkout.Session.retrieve(session_id)
    if session.payment_status != 'paid':
        flash('Payment was not successful.', 'danger')
        return redirect(url_for('buy_credits'))

    # Check if credits already added to avoid double credits on refresh
    if getattr(current_user, 'credits_purchased', None) != session.id:
        # Add credits to user (based on line_items quantity)
        # Stripe does not return line_items by default, fetch them:
        line_items = stripe.checkout.Session.list_line_items(session_id)
        total_credits = 0
        for item in line_items.data:
            total_credits += item.quantity

        current_user.credits += total_credits
        # Store session id on user to prevent double adding (optional)
        current_user.credits_purchased = session.id
        db.session.commit()

        flash(f'Successfully added {total_credits} credits to your account!', 'success')

    else:
        flash('Credits already added for this purchase.', 'info')

    return redirect(url_for('index'))

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
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/')
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
    transcript_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{pdf_filename}-transcript") # countintuitive labelling - fix
    summary_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{pdf_filename}-summary")
    math_transcript_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{pdf_filename}-math_transcript")


    transcript = transcribe_audio(audio_path, progress_callback)


    generate_math_pdf_from_transcipt(transcript,math_transcript_path, progress_callback)
    generate_formatted_transcript_from_base_transcript(transcript, transcript_path, progress_callback) # counterintuitive labelling - fix
    generate_summary_from_base_transcript(transcript, summary_path, progress_callback)
    progress_callback("[DONE]")

    # Create ZIP for download
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w') as zf:
        zf.write(transcript_path, arcname='transcript.pdf')
        zf.write(summary_path, arcname='summary.pdf')
        zf.write(math_transcript_path, arcname="math-transcript")
    memory_file.seek(0)

    return send_file(memory_file, as_attachment=True, download_name='audio_outputs.zip')


if __name__ == '__main__':
    print(db)
    app.run(debug=True, host='0.0.0.0')