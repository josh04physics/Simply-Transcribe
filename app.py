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


app = Flask(__name__, instance_relative_config=True)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SECRET_KEY'] = 'your_secret_key'

stripe.api_key = os.getenv("STRIPE_SECRET_KEY") # get stripe data (different locally and on render)
YOUR_DOMAIN = os.getenv("YOUR_DOMAIN")  # e.g. https://yourdomai


# work on setting database
# Database config: use DATABASE_URL from .env or Render
database_url = os.environ.get("DATABASE_URL")

if not database_url:
    os.makedirs(app.instance_path, exist_ok=True)
    db_path = os.path.join(app.instance_path, 'users.db')
    database_url = f"sqlite:///{db_path}"

# If using SQLite, ensure absolute path (fixes Render + Docker issues)
if database_url.startswith("sqlite:///"):
    db_path = database_url.replace("sqlite:///", "")
    db_path = os.path.abspath(db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    database_url = f"sqlite:///{db_path}"

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Init DB
db.init_app(app)

# Create DB tables if they don't exist (mainly for SQLite/local)
with app.app_context():
    if database_url.startswith("sqlite:///") and not os.path.exists(db_path):
        db.create_all()



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
def buy_credits(): # For specifying amount (confusing name, change later??)
    MIN_CREDITS = 250  # Minimum credits required to purchase

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
            metadata={
                "user_id": str(current_user.id),
                "credits": str(credits_to_buy)
            }
        )

        return redirect(session.url, code=303)

    return render_template('buy_credits.html')
@app.route("/create-checkout-session", methods=["POST"])
@login_required
def create_checkout_session(): # For bundle (confusing names, change later?)
    credit_amount = int(request.json["credits"])

    # Define pricing (pence, i.e. 1600 = £16.00)
    prices = {
        300: 540,
        500: 850,
        1000: 1600,
        2000: 2500
    }

    price = prices.get(credit_amount)
    if not price:
        return jsonify({"error": "Invalid bundle"}), 400

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "gbp",
                "product_data": {
                    "name": f"{credit_amount} Credit Bundle",
                },
                "unit_amount": price,
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=url_for("payment_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=url_for("buy_credits", _external=True),
        metadata={
            "user_id": str(current_user.id),  # IDs must be string
            "credits": str(credit_amount)
        }
    )

    return jsonify({"url": session.url})

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

    if getattr(current_user, 'credits_purchased', None) != session.id:
        # Get credits from metadata (as string, so convert)
        credits = int(session.metadata.get("credits", 0))

        current_user.credits += credits
        current_user.credits_purchased = session.id
        db.session.commit()

        flash(f'Successfully added {credits} credits to your account!', 'success')
    else:
        flash('Credits already added for this purchase.', 'info')

    return redirect(url_for('buy_credits'))

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

    # Get selected outputs from form checkboxes (list of strings)
    outputs = request.form.getlist('outputs')
    if not outputs:
        flash("Please select at least one output type.", "warning")
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

    # --- STEP 4: Transcribe audio ---
    transcript = transcribe_audio(audio_path, progress_callback)

    # Prepare base filename (without extension)
    base_filename = os.path.splitext(file.filename)[0]

    output_files = []

    if 'maths' in outputs:
        math_pdf_transcript_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{base_filename}-math_transcript.pdf")
        generate_math_pdf_from_transcipt(transcript, math_pdf_transcript_path, progress_callback)
        output_files.append((math_pdf_transcript_path, "math-transcript.pdf"))


        math_tex_transcript_path = os.path.join(app.config['UPLOAD_FOLDER'], f"Math_Transcription.tex")
        output_files.append((math_tex_transcript_path, "math-transcript.tex")) # TEMPORARY (maybe add functionallity to pdfgeneration) upload TEX file as well


    if 'transcript' in outputs:
        transcript_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{base_filename}-transcript.pdf")
        generate_formatted_transcript_from_base_transcript(transcript, transcript_path, progress_callback)
        output_files.append((transcript_path, "transcript.pdf"))


    if 'summary' in outputs:
        summary_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{base_filename}-summary.pdf")
        generate_summary_from_base_transcript(transcript, summary_path, progress_callback)
        output_files.append((summary_path, "summary.pdf"))


    progress_callback("[DONE]")

    # Create ZIP in-memory with only selected outputs
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w') as zf:
        for filepath, arcname in output_files:
            zf.write(filepath, arcname=arcname)
    memory_file.seek(0)

    return send_file(memory_file, as_attachment=True, download_name='audio_outputs.zip')

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')