#!/bin/bash
script_dir="/root/apps/reddit-post-notifier-script/"
running_file="reddit_post_notifier.running"

mail_sent_file="monitor_mail_sent.txt"

log_file="reddit_post_notifier.log"

cd $script_dir

pid=`cat $running_file`

kill -0 $pid
kill_ret=$?

if [ "$kill_ret" -ne "0" ] && [ ! -f $mail_sent_file ]
then
    echo "mail sent" > $mail_sent_file
    (echo "reddit_post_notifier LOG"; tail -40 $log_file;) | mail -t jjmerri88@gmail.com -s "reddit_post_notifier Not Running!"
fi

exit 0
