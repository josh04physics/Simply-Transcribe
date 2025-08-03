from flask import Flask, request, render_template, send_file, url_for, redirect, flash, Response, stream_with_context, jsonify
from flask_login import login_user, login_required, logout_user, LoginManager, current_user
from flask_migrate import Migrate
import os
from models import db
from models.user import User
from models.progress import Progress
from models.results import Results
from forms.forms import RegisterForm, LoginForm
from flask_bcrypt import Bcrypt
import sys
from pydub.utils import which
from pydub import AudioSegment
from dotenv import load_dotenv
import stripe
from concurrent.futures import ThreadPoolExecutor
import threading
from tasks import background_process_file, background_generate_outputs, download_youtube_audio
import io
import time
import uuid
import subprocess
import logging

load_dotenv()


app = Flask(__name__, instance_relative_config=True)

executor = ThreadPoolExecutor(max_workers=4)

app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')

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
def calculate_and_deduct_credits(audio_path, outputs):
    try:
        audio = AudioSegment.from_file(audio_path)
        duration_minutes = max(1, -(-len(audio) // 60000))
        num_outputs = len(outputs)
        total_credits_needed = duration_minutes * num_outputs
    except Exception as e:
        raise ValueError(f"Failed to read audio: {e}")

    if current_user.credits < total_credits_needed:
        raise PermissionError(f"You need {total_credits_needed} credits, "
                              f"but have {current_user.credits}.")

    current_user.credits -= total_credits_needed
    db.session.commit()
    return total_credits_needed, duration_minutes

# app.py
@app.route("/progress")
def progress():
    filename = request.args.get("filename")
    phase = request.args.get("phase", "phase1")

    if not filename:
        return jsonify({"error": "Missing filename"}), 400

    def generate_progress():
        seen_ids = set()
        while True:
            entries = Progress.query.filter_by(filename=filename, phase=phase).order_by(Progress.id).all()
            new_entries = [e for e in entries if e.id not in seen_ids]

            for entry in new_entries:
                seen_ids.add(entry.id)
                yield f"data: {entry.message}\n\n"
                if entry.is_done:
                    yield "data: [DONE]\n\n"
                    return
            time.sleep(1)

    response = Response(stream_with_context(generate_progress()), content_type="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"  # Important for Render and Nginx
    return response


@app.route('/buy-credits', methods=['GET', 'POST'])
@login_required
def buy_credits(): # For specifying amount (confusing name, change later??)
    MIN_CREDITS = 125  # Minimum credits required to purchase

    if request.method == 'POST':
        try:
            credits_to_buy = int(request.form.get('credits'))
            if credits_to_buy < MIN_CREDITS:
                flash(f'You must purchase at least {MIN_CREDITS} credits.', 'warning')
                return redirect(url_for('buy_credits'))
        except (TypeError, ValueError):
            flash('Invalid input for credits.', 'warning')
            return redirect(url_for('buy_credits'))

        price_per_credit_cents = 3

        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'gbp',
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
        200: 560,
        500: 1350,
        1000: 2600,
        2000: 5000
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
        # Check if email or username already exist
        existing_user_email = User.query.filter_by(email=form.email.data).first()
        if existing_user_email:
            flash("Email already registered. Try logging in instead.", "danger")
            return render_template('register.html', form=form)

        existing_user_username = User.query.filter_by(username=form.username.data).first()
        if existing_user_username:
            flash("Username already taken. Please choose another.", "danger")
            return render_template('register.html', form=form)

        # No conflicts, create user
        hashed_password = bcrypt.generate_password_hash(form.password.data).decode('utf-8')
        new_user = User(username=form.username.data,
                        email=form.email.data,
                        password=hashed_password,
                        credits=10)
        db.session.add(new_user)
        db.session.commit()
        flash("Account created successfully. You can now log in.", "success")
        return redirect(url_for('login'))

    return render_template('register.html', form=form)


@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        identifier = form.username_or_email.data.strip()
        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier)
        ).first()

        if not user:
            form.username_or_email.errors.append("Username or email not found.")
        elif not bcrypt.check_password_hash(user.password, form.password.data):
            form.password.errors.append("Incorrect password.")
        else:
            login_user(user)
            return redirect(url_for('index'))

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
    file = request.files.get('audio_file')
    if not file or file.filename == '':
        flash("No file selected", "danger")
        return redirect(url_for('index'))

    outputs = request.form.getlist('outputs')
    if not outputs:
        flash("Please select at least one output type.", "warning")
        return redirect(url_for('index'))

    audio_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
    file.save(audio_path)

    try:
        total_credits_needed, duration_minutes = calculate_and_deduct_credits(audio_path, outputs)
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(url_for('index'))
    except PermissionError as e:
        flash(str(e), "warning")
        return redirect(url_for('index'))

    flash(f"{total_credits_needed} credits deducted "
          f"({duration_minutes} min × {len(outputs)} outputs). "
          f"You have {current_user.credits} remaining.", "success")

    base_filename = os.path.splitext(file.filename)[0]

    # Fire off background task
    thread = threading.Thread(target=background_process_file, args=(app, audio_path, base_filename, outputs))
    thread.start()

    return render_template("processing.html", filename=base_filename)
@app.route('/upload_link', methods=['POST'])
@login_required
def upload_youtube_link():
    print("upload youtube link called")
    youtube_url = request.form.get("youtube_url")
    outputs = request.form.getlist('outputs')

    print("received url and outputs")

    if not outputs:
        flash("Please select at least one output type.", "warning")
        return redirect(url_for('index'))

    if not youtube_url:
        flash("YouTube URL is required.", "danger")
        return redirect(url_for('index'))

    # Temporary path for the audio file
    unique_id = uuid.uuid4().hex
    output_path = os.path.join("/tmp", f"{unique_id}.%(ext)s")

    # Build yt-dlp command (NO cookies)
    cmd = [
        "yt-dlp",
        youtube_url,
        "-x",
        "--audio-format", "mp3",
        "-o", output_path
    ]
    print("Running yt-dlp command:", " ".join(cmd))

    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        audio_path = output_path.replace("%(ext)s", "mp3")

        success, message = calculate_and_deduct_credits(audio_path, outputs)
        if not success:
            os.remove(audio_path)
            flash(message, "warning")
            print("Not enough credits, audio file removed.")
            return redirect(url_for('index'))
        flash(message, "success")
        print("Download successful, audio path:", audio_path)

    except subprocess.CalledProcessError as e:
        print("Error downloading video:", e, e.stderr)
        stderr = e.stderr or ""
        if "This video is age restricted" in stderr or "Sign in to confirm your age" in stderr or "HTTP Error 403" in stderr:
            flash("This video is age-restricted, private, or unavailable without login. Please try another video.", "danger")
        else:
            flash("Download failed. Please make sure the link is valid and the video is public.", "danger")
        return redirect(url_for('index'))

    except Exception as e:
        print(e)
        print("Error")
        flash(f"Unexpected error: {e}", "danger")
        return redirect(url_for('index'))

    # Background processing
    filename = os.path.splitext(os.path.basename(audio_path))[0]
    thread = threading.Thread(target=background_process_file, args=(app, audio_path, filename, outputs))
    thread.start()

    return render_template('processing.html', filename=filename)


@app.route('/check_results/<filename>')
@login_required
def check_results(filename):
    result = Results.query.filter_by(filename=filename).first()
    if result:
        return render_template(
            "edit_outputs.html",
            formatted_transcript=result.transcript,
            summary=result.summary,
            filename=filename,
            selected_outputs=result.outputs
        )
    else:
        return "", 202  # Not ready yet

@app.route('/finalize', methods=['POST'])
@login_required
def finalize_edits():
    edited_transcript = request.form.get("transcript", "").strip()
    edited_summary = request.form.get("summary", "").strip()
    filename = request.form.get("filename", "output").strip() or "output"
    outputs = request.form.getlist('outputs')

    if not edited_transcript and not edited_summary:
        return "No transcript or summary content to generate PDFs from.", 400

    thread = threading.Thread(target=background_generate_outputs, args=(
        app,
        edited_transcript,
        edited_summary,
        filename,
        outputs
    ))
    thread.start()

    return render_template("processing_final.html", filename=filename)

@app.route("/processing_final")
@login_required
def processing_final():
    filename = request.args.get("filename")
    return render_template("processing_final.html", filename=filename)


@app.route("/download_ready/<filename>")
@login_required
def download_ready(filename):
    result = Results.query.filter_by(filename=filename).first()
    if result and result.zip_ready:
        return "", 200
    return "", 202


@app.route('/download_zip/<filename>')
@login_required
def download_zip(filename):
    result = Results.query.filter_by(filename=filename).first()
    if not result or not result.zip_ready:
        return jsonify({"error": "ZIP file not ready"}), 202

    memory_file = io.BytesIO(result.zip_data)
    memory_file.seek(0)

    # Serve the ZIP file
    response = send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"{filename}_outputs.zip"
    )

    # Remove data from the database after serving the file
    try:
        Progress.query.filter_by(filename=filename, phase="phase1").delete()
        Progress.query.filter_by(filename=filename, phase="phase2").delete()
        db.session.delete(result)  # ✅ Delete the result entry

        db.session.commit()
    except Exception as e:
        print(f"Error deleting database entries: {e}")

    return response

@app.route("/success")
@login_required
def success():
    return render_template("success.html")


@app.route("/examples")
def examples():
    return render_template("examples.html")

if __name__ == '__main__':
    app.run(host='0.0.0.0')