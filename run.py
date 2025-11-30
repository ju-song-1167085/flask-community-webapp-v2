
from eventbridge_plus import app

# If run.py was actually executed (run), not just imported into another script,
# then start our Flask app on a local development server. To learn more about
# how we check for this, refer to https://realpython.com/if-name-main-python/.
if __name__ == "__main__":
    app.run(debug=True)