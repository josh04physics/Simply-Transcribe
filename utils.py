from models.user import User

def print_all_users():
    users = User.query.all()
    print(f"\n--- Total users: {len(users)} ---")
    for user in users:
        print(f"ID: {user.id}")
        print(f"Username: {user.username}")
        print(f"Email: {user.email}")
        print(f"Credits: {user.credits}")
        print("-" * 30)
