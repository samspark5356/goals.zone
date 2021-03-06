import datetime
import json
import operator
import os
import re
import time
import traceback
from datetime import date, timedelta
from functools import reduce
from xml.etree import ElementTree as ETree

import markdown as markdown
import requests
import tweepy
from background_task import background
from discord_webhook import DiscordWebhook
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db.models import Q
from django.utils import timezone
from slack_webhook import Slack

from matches.models import Match, VideoGoal, AffiliateTerm, VideoGoalMirror, Team
from monitoring.models import MatchNotFound, MonitoringAccount
from msg_events.models import Webhook, Tweet, MessageObject

TWITTER_CONSUMER_KEY = os.environ.get('TWITTER_CONSUMER_KEY')
TWITTER_CONSUMER_SECRET = os.environ.get('TWITTER_CONSUMER_SECRET')
TWITTER_ACCESS_TOKEN = os.environ.get('TWITTER_ACCESS_TOKEN')
TWITTER_ACCESS_TOKEN_SECRET = os.environ.get('TWITTER_ACCESS_TOKEN_SECRET')


@background(schedule=60)
def fetch_videogoals():
    print('Fetching new goals')
    _fetch_reddit_goals()
    # How to get historic data
    # _fetch_reddit_goals_from_date(days_ago=2)


def _fetch_reddit_goals():
    i = 0
    after = None
    while i < 10:
        response = _fetch_data_from_reddit_api(after)
        if response is None or response.content is None:
            print(f'No response retrieved')
            continue
        data = json.loads(response.content)
        if 'data' not in data.keys():
            print(f'No data in response: {response.content}')
            return
        results = data['data']['dist']
        print(f'{results} posts fetched...')
        for post in data['data']['children']:
            post = post['data']
            if post['url'] is not None and 'Thread' not in post['title'] and 'reddit.com' not in post['url']:
                title = post['title']
                find_and_store_videogoal(post, title)
        after = data['data']['after']
        i += 1
    print('Finished fetching goals')


def _fetch_reddit_goals_from_date(days_ago=2):
    start_date = date.today() - timedelta(days=days_ago)
    for single_date in (start_date + timedelta(n) for n in range(days_ago + 1)):
        response = _fetch_historic_data_from_reddit_api(single_date)
        data = json.loads(response.content)
        if 'data' not in data.keys():
            print(f'No data in response: {response.content}')
            return
        results = len(data['data'])
        print(f'{results} posts fetched...')
        for post in data['data']:
            if post['url'] is not None and 'Thread' not in post['title'] and 'reddit.com' not in post['url']:
                title = post['title']
                find_and_store_videogoal(post, title, single_date)
        print(f'Ended processing day {single_date}')
    print('Finished fetching goals')


def calculate_next_mirrors_check(videogoal):
    now = timezone.now()
    created_how_long = videogoal.created_at - now
    if created_how_long < timedelta(minutes=10):
        next_mirrors_check = now + datetime.timedelta(minutes=1)
        videogoal.next_mirrors_check = next_mirrors_check
        return
    if created_how_long < timedelta(minutes=30):
        next_mirrors_check = now + datetime.timedelta(minutes=5)
        videogoal.next_mirrors_check = next_mirrors_check
        return
    if created_how_long < timedelta(minutes=60):
        next_mirrors_check = now + datetime.timedelta(minutes=10)
        videogoal.next_mirrors_check = next_mirrors_check
        return
    if created_how_long < timedelta(minutes=120):
        next_mirrors_check = now + datetime.timedelta(minutes=20)
        videogoal.next_mirrors_check = next_mirrors_check
        return
    if created_how_long < timedelta(minutes=240):
        next_mirrors_check = now + datetime.timedelta(minutes=30)
        videogoal.next_mirrors_check = next_mirrors_check
        return
    next_mirrors_check = now + datetime.timedelta(minutes=60)
    videogoal.next_mirrors_check = next_mirrors_check


def find_mirrors(videogoal):
    try:
        if videogoal.next_mirrors_check > timezone.now():
            return
        calculate_next_mirrors_check(videogoal)
        main_comments_link = 'http://api.reddit.com' + videogoal.permalink
        response = _make_reddit_api_request(main_comments_link)
        data = json.loads(response.content)
        try:
            for child in data[1]['data']['children']:
                if 'author' in child['data'] and child['data']['author'] == 'AutoModerator':
                    children_url = main_comments_link + child['data']['id']
                    children_response = _make_reddit_api_request(children_url)
                    children = json.loads(children_response.content)
                    if "replies" in children[1]['data']['children'][0]['data'] and isinstance(
                            children[1]['data']['children'][0]['data']['replies'], dict):
                        replies = children[1]['data']['children'][0]['data']['replies']['data']['children']
                        for reply in replies:
                            _parse_reply_for_mirrors(reply, videogoal)
        except Exception as e:
            tb = traceback.format_exc()
            print(tb)
            print(e)
    except Exception as e:
        print("An exception as occurred trying to find mirrors", e)


def _parse_reply_for_mirrors(reply, videogoal):
    body = reply['data']['body']
    author = reply['data']['author']
    stripped_body = os.linesep.join([s for s in body.splitlines() if s])
    try:
        doc = ETree.fromstring(markdown.markdown(stripped_body))
    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        print(e)
    else:
        links = doc.findall('.//a')
        if len(links) > 0:
            _extract_links_from_comment(author, links, videogoal)
        else:
            _extract_urls_from_comment(author, body, videogoal)


def _extract_urls_from_comment(author, body, videogoal):
    for line in body.splitlines():
        urls = re.findall(
            r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*(),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+',
            line)
        if len(urls) > 0:
            for url in urls:
                val = URLValidator()
                try:
                    val(url)
                    text = line.replace(url, '')
                    if ':' in text:
                        text = text.split(':', 1)[0]
                    _insert_or_update_mirror(videogoal, text, url, author)
                except ValidationError:
                    pass


def _extract_links_from_comment(author, links, videogoal):
    for link in links:
        val = URLValidator()
        try:
            val(link.get('href'))
            text = link.text
            if 'http' in text and link.tail is not None and len(link.tail) > 0:
                text = link.tail
            _insert_or_update_mirror(videogoal, text, link.get('href'), author)
        except ValidationError:
            pass


def _insert_or_update_mirror(videogoal, text, url, author):
    try:
        mirror = VideoGoalMirror.objects.get(url__exact=url, videogoal__exact=videogoal)
    except VideoGoalMirror.DoesNotExist:
        mirror = VideoGoalMirror()
        mirror.url = url
        mirror.videogoal = videogoal
    if len(re.sub(r"[\r\n\t\s]*", "", text)) == 0:
        text = None
    if text is not None:
        mirror.title = (text[:195] + '..') if len(text) > 195 else text
    else:
        mirror.title = None
    mirror.author = author
    mirror.save()
    if not mirror.msg_sent and \
            mirror.videogoal.match.home_team.name_code is not None and \
            mirror.videogoal.match.away_team.name_code is not None:
        send_messages(mirror.videogoal.match, None, mirror, MessageObject.MessageEventType.Mirror)


def send_messages(match, videogoal, videogoal_mirror, event_filter):
    send_tweet(match, videogoal, videogoal_mirror, event_filter)
    send_discord_webhook_message(match, videogoal, videogoal_mirror, event_filter)
    send_slack_webhook_message(match, videogoal, videogoal_mirror, event_filter)
    if MessageObject.MessageEventType.Match == event_filter and match is not None:
        match.msg_sent = True
        match.save()
    if MessageObject.MessageEventType.Video == event_filter and videogoal is not None:
        videogoal.msg_sent = True
        videogoal.save()
    if MessageObject.MessageEventType.Mirror == event_filter and videogoal_mirror is not None:
        videogoal_mirror.msg_sent = True
        videogoal_mirror.save()


def format_event_message(match, videogoal, videogoal_mirror, message):
    message = message.format(m=match, vg=videogoal, vgm=videogoal_mirror)
    return message


def check_conditions(match, msg_obj):
    if msg_obj.include_categories.all().count() > 0 and \
            (match.category is None or not msg_obj.include_categories.filter(id=match.category.id).exists()):
        return False
    if msg_obj.include_tournaments.all().count() > 0 and \
            (match.tournament is None or not msg_obj.include_tournaments.filter(id=match.tournament.id).exists()):
        return False
    if msg_obj.exclude_categories.all().count() > 0 and \
            (match.category is None or msg_obj.exclude_categories.filter(id=match.category.id).exists()):
        return False
    if msg_obj.exclude_tournaments.all().count() > 0 and \
            (match.tournament is None or msg_obj.exclude_tournaments.filter(id=match.tournament.id).exists()):
        return False
    return True


def send_slack_webhook_message(match, videogoal, videogoal_mirror, event_filter):
    try:
        webhooks = Webhook.objects.filter(destination__exact=Webhook.WebhookDestinations.Slack,
                                          event_type=event_filter)
        for wh in webhooks:
            to_send = check_conditions(match, wh) and \
                      check_link_regex(wh, videogoal, videogoal_mirror, event_filter) and \
                      check_author(wh, videogoal, videogoal_mirror, event_filter)
            if not to_send:
                return
            message = format_event_message(match, videogoal, videogoal_mirror, wh.message)
            try:
                slack = Slack(url=wh.webhook_url)
                response = slack.post(text=message)
                print(response)
            except Exception as ex:
                print("Error sending webhook single message: " + str(ex))
    except Exception as ex:
        print("Error sending webhook messages: " + str(ex))


def send_discord_webhook_message(match, videogoal, videogoal_mirror, event_filter):
    try:
        webhooks = Webhook.objects.filter(destination__exact=Webhook.WebhookDestinations.Discord,
                                          event_type=event_filter)
        for wh in webhooks:
            to_send = check_conditions(match, wh) and \
                      check_link_regex(wh, videogoal, videogoal_mirror, event_filter) and \
                      check_author(wh, videogoal, videogoal_mirror, event_filter)
            if not to_send:
                return
            message = format_event_message(match, videogoal, videogoal_mirror, wh.message)
            try:
                webhook = DiscordWebhook(url=wh.webhook_url, content=message)
                response = webhook.execute()
                print(response)
            except Exception as ex:
                print("Error sending webhook single message: " + str(ex))
    except Exception as ex:
        print("Error sending webhook messages: " + str(ex))


def check_link_regex(msg_obj, videogoal, videogoal_mirror, event_filter):
    if MessageObject.MessageEventType.Video == event_filter and videogoal is not None:
        if msg_obj.link_regex is not None and len(msg_obj.link_regex) > 0:
            pattern = re.compile(msg_obj.link_regex)
            if not pattern.match(videogoal.url):
                return False
    if MessageObject.MessageEventType.Mirror == event_filter and videogoal_mirror is not None:
        if msg_obj.link_regex is not None and len(msg_obj.link_regex) > 0:
            pattern = re.compile(msg_obj.link_regex)
            if not pattern.match(videogoal_mirror.url):
                return False
    return True


def check_author(msg_obj, videogoal, videogoal_mirror, event_filter):
    if MessageObject.MessageEventType.Video == event_filter and videogoal is not None:
        if msg_obj.author_filter is not None and len(msg_obj.author_filter) > 0:
            if videogoal.author != msg_obj.author_filter:
                return False
    if MessageObject.MessageEventType.Mirror == event_filter and videogoal_mirror is not None:
        if msg_obj.author_filter is not None and len(msg_obj.author_filter) > 0:
            if videogoal_mirror.author != msg_obj.author_filter:
                return False
    return True


def send_tweet(match, videogoal, videogoal_mirror, event_filter):
    try:
        tweets = Tweet.objects.filter(event_type=event_filter)
        for tw in tweets:
            to_send = check_conditions(match, tw) and \
                      check_link_regex(tw, videogoal, videogoal_mirror, event_filter) and \
                      check_author(tw, videogoal, videogoal_mirror, event_filter)
            if not to_send:
                return
            try:
                message = format_event_message(match, videogoal, videogoal_mirror, tw.message)
                auth = tweepy.OAuthHandler(tw.consumer_key, tw.consumer_secret)
                auth.set_access_token(tw.access_token_key, tw.access_token_secret)
                api = tweepy.API(auth)
                result = api.update_status(status=message)
                print(result)
            except Exception as ex:
                print("Error sending twitter single message", str(ex))
                send_monitoring_message("*Twitter message not sent!*\n" + str(ex))
    except Exception as ex:
        print("Error sending twitter messages: " + str(ex))


def send_telegram_message(bot_key, user_id, message, disable_notification=False):
    try:
        url = f'https://api.telegram.org/bot{bot_key}/sendMessage'
        msg_obj = {
            "chat_id": user_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_notification": disable_notification
        }
        resp = requests.post(url, data=msg_obj)
        print(resp)
    except Exception as ex:
        print("Error sending monitoring message: " + str(ex))


def send_monitoring_message(message, disable_notification=False):
    try:
        monitoring_accounts = MonitoringAccount.objects.all()
        for ma in monitoring_accounts:
            send_telegram_message(ma.telegram_bot_key, ma.telegram_user_id, message, disable_notification)
    except Exception as ex:
        print("Error sending monitoring message: " + str(ex))


def find_and_store_videogoal(post, title, match_date=date.today()):
    home_team, away_team, minute_str = extract_names_from_title(title)
    if home_team is None or away_team is None:
        return
    matches_results = find_match(home_team, away_team, from_date=match_date)
    if matches_results.exists():
        _save_found_match(matches_results, minute_str, post)
    else:
        try:
            _handle_not_found_match(away_team, home_team, post)
        except Exception as ex:
            print("Exception in monitoring: " + str(ex))


def _save_found_match(matches_results, minute_str, post):
    match = matches_results.first()
    # print(f'Match {match} found for: {title}')
    try:
        videogoal = VideoGoal.objects.get(permalink__exact=post['permalink'])
    except VideoGoal.DoesNotExist:
        videogoal = VideoGoal()
        videogoal.permalink = post['permalink']
        videogoal.next_mirrors_check = timezone.now()
    videogoal.match = match
    videogoal.url = post['url']
    videogoal.title = (post['title'][:195] + '..') if len(post['title']) > 195 else post['title']
    videogoal.minute = minute_str.strip()[:12]
    videogoal.author = post['author']
    videogoal.save()
    _handle_messages_to_send(match, videogoal)
    find_mirrors(videogoal)
    # print('Saved: ' + title)


def _handle_messages_to_send(match, videogoal):
    if not videogoal.msg_sent and \
            match.home_team.name_code is not None and \
            match.away_team.name_code is not None:
        send_messages(match, videogoal, None, MessageObject.MessageEventType.Video)
    if match.videogoal_set.count() > 0 and \
            not match.msg_sent and \
            match.home_team.name_code is not None and \
            match.away_team.name_code is not None:
        send_messages(match, None, None, MessageObject.MessageEventType.Match)


def _handle_not_found_match(away_team, home_team, post):
    try:
        MatchNotFound.objects.get(permalink__exact=post['permalink'])
    except MatchNotFound.DoesNotExist:
        d = datetime.datetime.utcnow()
        epoch = datetime.datetime(1970, 1, 1)
        t = (d - epoch).total_seconds()
        home_team_obj = Team.objects.filter(Q(name__unaccent__trigram_similar=home_team) |
                                            Q(alias__alias__unaccent__trigram_similar=home_team))
        away_team_obj = Team.objects.filter(Q(name__unaccent__trigram_similar=away_team) |
                                            Q(alias__alias__unaccent__trigram_similar=away_team))
        if (t - post['created_utc']) < 86400:  # in the last day
            match_not_found = MatchNotFound()
            match_not_found.permalink = post['permalink']
            match_not_found.title = (post['title'][:195] + '..') if len(post['title']) > 195 else post['title']
            match_not_found.home_team_str = home_team
            match_not_found.away_team_str = away_team
            match_not_found.save()
            if home_team_obj or away_team_obj:
                send_monitoring_message(
                    f"__Match not found in database__\n*{home_team}*\n*{away_team}*\n{post['title']}", True)


def extract_names_from_title(title):
    # Maybe later we should consider the format
    # HOME_TEAM - AWAY_TEAM HOME_SCORE-AWAY_SCORE
    home = re.findall(r'\[?\]?\s?((\w|\s|\.|-)+)((\d|\[\d\])([-x]| [-x] | [-x]|[-x] ))(\d|\[\d\])', title)
    away = re.findall(r'(\d|\[\d\])([-x]| [-x] | [-x]|[-x] )(\d|\[\d\])\s?(((\w|\s|\.|-)(?!- ))+)(:|\s?\||-)?',
                      title)
    minute = re.findall(r'(\S*\d+\S*)\'', title)
    if len(home) > 0:
        home_team = home[0][0].strip()
        if len(away) > 0:
            away_team = away[0][3].strip()
            if len(minute) > 0:
                minute_str = minute[-1].strip()
            else:
                minute_str = ''
                print(f'Minute not found for: {title}')
            return home_team, away_team, minute_str
        else:
            print('Failed away: ' + title)
    else:
        print('Failed home and away: ' + title)
    return None, None, None


def find_match(home_team, away_team, from_date=date.today()):
    suffix_affiliate_terms = AffiliateTerm.objects.filter(is_prefix=False).values_list('term', flat=True)
    suffix_regex_string = r'( ' + r'| '.join(suffix_affiliate_terms) + r')$'
    prefix_affiliate_terms = AffiliateTerm.objects.filter(is_prefix=True).values_list('term', flat=True)
    prefix_regex_string = r'^(' + r' |'.join(prefix_affiliate_terms) + r' )'
    suffix_affiliate_home = re.findall(suffix_regex_string, home_team)
    suffix_affiliate_away = re.findall(suffix_regex_string, away_team)
    prefix_affiliate_home = re.findall(prefix_regex_string, home_team)
    prefix_affiliate_away = re.findall(prefix_regex_string, away_team)
    matches = Match.objects.filter(Q(home_team__name__unaccent__trigram_similar=home_team) |
                                   Q(home_team__alias__alias__unaccent__trigram_similar=home_team),
                                   Q(away_team__name__unaccent__trigram_similar=away_team) |
                                   Q(away_team__alias__alias__unaccent__trigram_similar=away_team),
                                   datetime__gte=(from_date - timedelta(days=2)))
    if len(suffix_affiliate_home) > 0:
        matches = matches.filter(home_team__name__endswith=suffix_affiliate_home[0])
    else:
        matches = matches.exclude(
            reduce(operator.or_, (Q(home_team__name__endswith=f' {term}') for term in suffix_affiliate_terms)))
    if len(prefix_affiliate_home) > 0:
        matches = matches.filter(home_team__name__startswith=prefix_affiliate_home[0])
    else:
        matches = matches.exclude(
            reduce(operator.or_, (Q(home_team__name__startswith=f' {term}') for term in prefix_affiliate_terms)))

    if len(suffix_affiliate_away) > 0:
        matches = matches.filter(away_team__name__endswith=suffix_affiliate_away[0])
    else:
        matches = matches.exclude(
            reduce(operator.or_, (Q(away_team__name__endswith=f' {term}') for term in suffix_affiliate_terms)))
    if len(prefix_affiliate_away) > 0:
        matches = matches.filter(away_team__name__startswith=prefix_affiliate_away[0])
    else:
        matches = matches.exclude(
            reduce(operator.or_, (Q(away_team__name__startswith=f' {term}') for term in prefix_affiliate_terms)))
    return matches


def _fetch_data_from_reddit_api(after):
    headers = {
        "User-agent": "Goals Populator 0.1"
    }
    response = requests.get(f'http://api.reddit.com/r/soccer/new?limit=100&after={after}',
                            headers=headers)
    return response


def _make_reddit_api_request(link):
    headers = {
        "User-agent": "Goals Populator 0.1"
    }
    response = requests.get(link, headers=headers)
    return response


def _fetch_historic_data_from_reddit_api(from_date):
    after = int(time.mktime(from_date.timetuple()))
    before = int(after + 86400)  # a day
    headers = {
        "User-agent": "Goals Populator 0.1"
    }
    response = requests.get(
        f'https://api.pushshift.io/reddit/search/submission/'
        f'?subreddit=soccer&sort=desc&sort_type=created_utc&after={after}&before={before}&size=1000',
        headers=headers)
    return response
