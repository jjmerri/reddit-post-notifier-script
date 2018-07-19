#!/usr/bin/env python3.6

# =============================================================================
# IMPORTS
# =============================================================================
import praw
import configparser
import logging
import time
import os
import smtplib

from email.mime.text import MIMEText
from threading import Thread, Lock, Event, current_thread
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

# Setup firebase connection/
scopes = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/firebase.database"
]
credentials = service_account.Credentials.from_service_account_file(
    "./serviceAccountKey.json", scopes=scopes)
authed_session = AuthorizedSession(credentials)

# =============================================================================
# GLOBALS
# =============================================================================

# Reads the config file
config = configparser.ConfigParser()
config.read("reddit_post_notifier.cfg")

bot_username = config.get("Reddit", "username")
bot_password = config.get("Reddit", "password")
client_id = config.get("Reddit", "client_id")
client_secret = config.get("Reddit", "client_secret")

# Reddit info
reddit = praw.Reddit(client_id=client_id,
                     client_secret=client_secret,
                     password=bot_password,
                     user_agent='reddit_post_notifier by /u/BoyAndHisBlob',
                     username=bot_username)

EMAIL_SERVER = config.get("EMAIL", "server")
EMAIL_USERNAME = config.get("EMAIL", "username")
EMAIL_PASSWORD = config.get("EMAIL", "password")

DEV_EMAIL = config.get("REDDITPOSTNOTIFIER", "dev_email")

LAST_SUBMISSION_FILE = "lastsubmission.txt"

last_submission_lock = Lock()

pm_notification_subject = "New Post In {subreddit_name}"
pm_notification_body = "{permalink}"
last_submission_sec = {}

RUNNING_FILE = "reddit_post_notifier.running"
ENVIRONMENT = config.get("REDDITPOSTNOTIFIER", "environment")
DEV_USER_NAME = config.get("REDDITPOSTNOTIFIER", "dev_user")
FIREBASE_URI = config.get("REDDITPOSTNOTIFIER", "firebase_uri")

# Setup firebase connection
scopes = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/firebase.database"
]
credentials = service_account.Credentials.from_service_account_file(
    "./serviceAccountKey.json", scopes=scopes)
authed_session = AuthorizedSession(credentials)

FORMAT = '%(asctime)-15s %(message)s'
logging.basicConfig(format=FORMAT)
logger = logging.getLogger('redditPostNotifier')
logger.setLevel(logging.INFO)


class StoppableThread(Thread):
    """Thread class with a stop() method. The thread itself has to check
    regularly for the stopped() condition."""

    def __init__(self, target, args):
        super(StoppableThread, self).__init__(target=target, args=args)
        self.safe_to_stop = True


def get_sub_preferences(subreddit_name):
    response = authed_session.get(
        "{firebase_uri}/notification_preferences/subreddits/{subreddit_name}/user_preferences.json".format(
            firebase_uri=FIREBASE_URI,
            subreddit_name=subreddit_name
        ))

    if response is not None:
        return response.json()
    else:
        return None


def get_all_users_preferences():
    response = authed_session.get("{firebase_uri}/notification_preferences/users.json".format(
        firebase_uri=FIREBASE_URI
    ))

    if response is not None:
        return response.json()
    else:
        return None


def send_dev_pm(subject, body):
    """
    Sends Reddit PM to DEV_USER_NAME
    :param subject: subject of PM
    :param body: body of PM
    """
    reddit.redditor(DEV_USER_NAME).message(subject, body)


def listenForPosts(subreddit_name):
    calling_thread = current_thread()
    subreddit = reddit.subreddit(subreddit_name)
    start_time = last_submission_sec.get(subreddit_name, 0)
    if start_time == 0:
        start_time = time.time()

    unsent_submissions = set() #used to retry sending notifications if there was a failure
    # retry in case there was a temporary network issue
    retry_count = 0
    max_retires = 5
    retry_necessary = True
    while retry_count < max_retires and retry_necessary:
        try:
            retry_necessary = False

            for submission in subreddit.stream.submissions():
                if not os.path.isfile(RUNNING_FILE) or submission.created_utc <= start_time or (
                    time.time() - submission.created_utc) > 1800:
                    calling_thread.safe_to_stop = True
                    continue
                try:
                    unsent_submissions.add(submission)
                    calling_thread.safe_to_stop = False

                    sent_submissions = set()

                    #if there are multiple items in the unsent_submissions this could cause items to get resent
                    # if a failure occured on any element other than the first because they never get removed
                    for unsent_submission in unsent_submissions:
                        send_notifications(unsent_submission)
                        sent_submissions.add(unsent_submission)

                    for sent_submission in sent_submissions:
                        unsent_submissions.remove(sent_submission)

                    # reset count on successful call
                    retry_count = 0
                except Exception as err:
                    logger.exception("Unknown Exception sending notifications")
                    try:
                        send_dev_email("Error sending notifications", "Error: {exception}".format(exception=str(err)),
                                       [DEV_EMAIL])
                        send_dev_pm("Unknown Exception sending notifications",
                                    "Error: {exception}".format(exception=str(err)))
                    except Exception as err:
                        logger.exception("Unknown error sending dev pm or email")

                write_last_submission_time(subreddit_name, submission.created_utc)
                calling_thread.safe_to_stop = True

        except Exception as err:
            retry_count += 1
            retry_necessary = True
            logger.exception(
                "Unknown Exception listening for posts in {subreddit_name}. retry count: {retry_count}".format(
                    subreddit_name=subreddit_name, retry_count=str(retry_count)))
            if retry_count >= max_retires:
                try:
                    send_dev_email("Unknown Exception listening for posts in {subreddit_name}".format(
                        subreddit_name=subreddit_name),
                                   "Max Retries Reached!  Error: {exception}".format(exception=str(err)), [DEV_EMAIL])
                    send_dev_pm("Unknown Exception listening for posts",
                                "Max Retries Reached! Error: {exception}".format(exception=str(err)))
                except Exception as err:
                    logger.exception("Unknown error sending dev pm or email")

            calling_thread.safe_to_stop = True
            time.sleep(60)

    calling_thread.safe_to_stop = True


def send_notifications(submission):
    preferences = get_sub_preferences(submission.subreddit)
    emails = []
    if preferences:
        for user in preferences:
            if preferences[user]['emailNotification']:
                emails.append(get_user_email(user))
        if emails:
            send_email_notifications(submission.subreddit, submission.permalink, emails)


def get_user_email(user):
    all_users_prefernces = get_all_users_preferences()
    return all_users_prefernces[user]['global_preferences']['email']


def load_last_submission_times():
    last_submission_file = open(LAST_SUBMISSION_FILE, "r")
    for last_submission in last_submission_file.read().splitlines():
        values = last_submission.split(" ")
        if len(values) == 2:
            last_submission_sec[values[0]] = int(values[1])


def get_subscribed_subs():
    response = authed_session.get(
        "{firebase_uri}/supported_subreddits.json".format(
            firebase_uri=FIREBASE_URI
        ))

    if response is not None:
        return response.json()
    else:
        send_dev_email("Could Not Load Supported Subreddits", "Try to restart it manually.", [DEV_EMAIL])
        return []


def write_last_submission_time(subreddit_name, time_sec):
    with last_submission_lock:
        last_submission_sec[subreddit_name] = int(float(time_sec))
        last_submissions = ""
        for last_submission in last_submission_sec:
            last_submissions += last_submission + " " + str(
                last_submission_sec.get(last_submission, "10000")) + "\n"

        lastrun_file = open(LAST_SUBMISSION_FILE, "w")
        lastrun_file.write(last_submissions)
        lastrun_file.close()


def send_email_notifications(subreddit_name, permalink, email_addresses):
    sent_from = 'redditpostnotificationbot@gmail.com'
    subject = 'New Reddit Post Notification'
    footer = 'Manage your notification preferences at https://reddit-post-notifier.firebaseapp.com/home'
    body = 'New post in {subreddit_name}.\n\nhttps://www.reddit.com{permalink}\n\n{footer}'.format(
        subreddit_name=subreddit_name, permalink=permalink, footer=footer)

    msg = MIMEText(body.encode('utf-8'), 'plain', 'UTF-8')
    msg['Subject'] = subject

    server = smtplib.SMTP_SSL(EMAIL_SERVER, 465)
    server.ehlo()
    server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
    server.sendmail(sent_from, email_addresses, msg.as_string())
    server.close()


def send_dev_email(subject, body, email_addresses):
    sent_from = 'redditpostnotificationbot@gmail.com'

    msg = MIMEText(body.encode('utf-8'), 'plain', 'UTF-8')
    msg['Subject'] = subject

    server = smtplib.SMTP_SSL(EMAIL_SERVER, 465)
    server.ehlo()
    server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
    server.sendmail(sent_from, email_addresses, msg.as_string())
    server.close()


def create_running_file():
    running_file = open(RUNNING_FILE, "w")
    running_file.write(str(os.getpid()))
    running_file.close()


# =============================================================================
# MAIN
# =============================================================================

def main():
    logger.info("start")

    start_process = False
    max_restart_attempts = 5
    start_attempts = 0
    attempt_restart = True

    subscribed_subs = get_subscribed_subs()

    if ENVIRONMENT == "DEV" and os.path.isfile(RUNNING_FILE):
        os.remove(RUNNING_FILE)
        logger.info("running file removed")

    if not os.path.isfile(RUNNING_FILE):
        create_running_file()
        start_process = True
    else:
        start_process = False
        logger.error("reddit post notifier already running! Will not start.")

    if start_process:
        while attempt_restart and start_attempts <= max_restart_attempts:
            if start_attempts > 0:
                logger.info("Attempting restart {start_attempts}".format(start_attempts=start_attempts))
                try:
                    send_dev_email("Reddit Post Notifier Restarted Main Loop",
                               "Restart attempt number {start_attempts}".format(start_attempts=start_attempts), [DEV_EMAIL])
                except Exception as err:
                    logger.exception("Unknown error sending dev email about restart")

            start_attempts += 1
            attempt_restart = False
            if not os.path.isfile(LAST_SUBMISSION_FILE):
                last_submission_file = open(LAST_SUBMISSION_FILE, "w")
                last_submission_file.write(str(""))
                last_submission_file.close()

            load_last_submission_times()

            listening_threads = []

            for subreddit_name in subscribed_subs:
                t = StoppableThread(target=listenForPosts, args=[subreddit_name])
                t.daemon = True
                t.start()
                listening_threads.append(t)

            dead_thread_email_sent = False
            while os.path.isfile(RUNNING_FILE) and not attempt_restart:
                logger.info("running file present - waiting 60 secs")
                for thread in listening_threads:
                    if not thread.is_alive() and not dead_thread_email_sent:
                        logger.info("A thread is unexpectetedly dead. Attempting restart.")
                        try:
                            send_dev_email("Reddit Post Notifier Thread Dead",
                                       "A thread is unexpectetedly dead. Attempting restart.", [DEV_EMAIL])
                        except Exception as err:
                            logger.exception("Unknown error sending dev email about attempting restart")
                        dead_thread_email_sent = True
                        attempt_restart = True
                if not attempt_restart:
                    time.sleep(60)

            # wait till safe to exit program.
            wait_count = 0
            wait_to_exit = True
            while wait_to_exit:
                wait_to_exit = False
                for thread in listening_threads:
                    if not thread.safe_to_stop and thread.is_alive():
                        wait_to_exit = True

                # this should never happen but just in case something screwy happens don't wait forever
                if wait_count >= 10:
                    logger.info("max wait count hit - exiting program before all child threads complete!")
                    wait_to_exit = False
                wait_count += 1

                if wait_to_exit:
                    logger.info("waiting to exit - {wait_count}".format(wait_count=wait_count))
                    time.sleep(5)

    logger.info("end")


# =============================================================================
# RUNNER
# =============================================================================

if __name__ == '__main__':
    main()
