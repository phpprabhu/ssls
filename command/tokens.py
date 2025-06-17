import click
import config
import pandas as pd
import urllib.request
from sslsapp import app, db
import exchange.angel as angel
from sslsapp.models.model import Options, LastRun, Balance, Indexes, Orders
from datetime import datetime, timedelta, date
import math
import json
import helper.date_ist as date_ist


@click.command(name='reset')
@click.pass_context
def reset_options(ctx):
    db.session.query(Options).delete()
    db.session.commit()

    ctx.invoke(fetch_option_token)
    print('Reset Options')

    db.session.query(Orders).filter(Orders.status == "open").update(
        {Orders.status: "COMPLETE"}, synchronize_session=False
    )
    db.session.commit()
    print('Updated Open Orders to complete')


@click.command(name='fetch_option_token')
def fetch_option_token():
    angel_obj = angel.get_angel_obj()
    timeframe = '3m'
    [nse_interval, nse_max_days_per_interval, is_custom_interval] = angel.get_angel_timeframe_details(timeframe)

    symbol_file = urllib.request.urlopen(
        'https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json')
    symbols = json.load(symbol_file)
    tocken_df = pd.DataFrame.from_dict(symbols)
    # tocken_df['expiry'] = pd.to_datetime(tocken_df['expiry'], format='%d%b%Y', errors='coerce')
    tocken_df['expiry'] = pd.to_datetime(tocken_df['expiry'])
    tocken_df = tocken_df.astype({'strike': float})
    tocken_df['strike'] = tocken_df['strike'] / 100
    tocken_df.sort_values(by='strike', inplace=True)

    df_option_index_data = tocken_df.loc[tocken_df['instrumenttype'] == 'OPTIDX']

    indexes = Indexes.query.filter_by(enabled=True).all()

    for index in indexes:
        print('Fetching options of index: ' + index.name)
        # df_stock = angel.get_historical_data(angel_obj, index.token, timeframe, nse_interval, 750)
        #
        # index_ltp = round_to_nearest(df_stock.iloc[-1]['close'], index.option_sizing)
        # print(index_ltp)

        index_options = df_option_index_data[df_option_index_data['name'] == index.name]

        for i, option_data in index_options.iterrows():
            # if option_data.symbol[-2] + option_data.symbol[-1] == "PE":
            #     continue

            option_exists = Options.query.filter_by(instrument_token=option_data.token).first()

            if not option_exists:
                if (index.exp == "M" and option_data.expiry <= last_thursday_or_next_month()) or (index.exp == "W" and option_data.expiry <= get_next_thursday()):
                    option = Options(symbol=option_data.symbol, name=option_data['name'],
                                     instrument_token=int(option_data.token),
                                     exchange_token=0,
                                     segment='NFO', instrument_type=option_data.symbol[-2] + option_data.symbol[-1],
                                     lot_size=option_data.lotsize, strike=float(option_data.strike),
                                     expiry=option_data.expiry, exchange=index.exchange)

                    option.atm = False
                    option.near = False

                    # if (option.instrument_type == 'CE' and (index_ltp - (2 * index.option_sizing)) <= option.strike <= (
                    #         index_ltp + (2 * index.option_sizing))):
                    #     option.near = True
                    #
                    # if index_ltp == option.strike and option.instrument_type == 'CE':
                    #     option.atm = True
                    #
                    # if index_ltp == option.strike and option.instrument_type == 'PE':
                    #     option.atm = True

                    db.session.add(option)
    db.session.commit()

    last_run = LastRun.query.filter_by(cron='ALL-OPTIONS').first()
    last_run.ran_date = datetime.now()

    profile = angel_obj.rmsLimit()['data']
    fund_available = float(profile['utilisedpayout'])
    balance = Balance(balance=fund_available, when='ALL-OPTIONS')

    db.session.add(balance)

    db.session.commit()
    print('Options list updated in DB')


@click.command(name='update_near_token')
def update_near_token():
    if str(date.today()) in config.HOLIDAYS:
        exit()

    angel_obj = angel.get_angel_obj()
    timeframe = '3m'
    [nse_interval, nse_max_days_per_interval, is_custom_interval] = angel.get_angel_timeframe_details(timeframe)

    indexes = Indexes.query.filter_by(enabled=True).all()

    current_datetime = datetime.today()

    for index in indexes:
        print('Fetching options of index: ' + index.name)
        df_stock = angel.get_historical_data(angel_obj, index.token, timeframe, nse_interval, 750)

        index_ltp = round_to_nearest(df_stock.iloc[-1]['close'], index.option_sizing)
        print(index_ltp)
        options = Options.query.filter_by(ws_remove=False, name=index.name).all()

        for option in options:
            option.atm = False
            option.near = False

            if (option.instrument_type == 'CE' and (index_ltp - (2 * index.option_sizing)) <= option.strike <= (
                    index_ltp + (2 * index.option_sizing))) or (
                    option.instrument_type == 'PE' and (index_ltp - index.option_sizing) <= option.strike <= (
                    index_ltp + (3 * index.option_sizing))):
                option.near = True

            if index_ltp == option.strike and option.instrument_type == 'CE':
                option.atm = True

            if index_ltp == option.strike and option.instrument_type == 'PE':
                option.atm = True

            db.session.add(option)

    last_run = LastRun.query.filter_by(cron='NEAR').first()
    last_run.ran_date = datetime.now()

    profile = angel_obj.rmsLimit()['data']
    fund_available = float(profile['utilisedpayout'])
    balance = Balance(balance=fund_available, when='FAR')

    db.session.add(balance)

    db.session.commit()
    print('Options list updated with NEAR in DB')


from datetime import datetime, timedelta


def get_last_thursday(year, month):
    # Get the last day of the given month
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    last_day_of_month = next_month - timedelta(days=1)

    # Find the last Wednesday
    while last_day_of_month.weekday() != 3:  # 3 represents Thursday
        last_day_of_month -= timedelta(days=1)

    return last_day_of_month


def last_thursday_or_next_month():
    today = datetime.today()
    year, month = today.year, today.month

    # Get the last Wednesday of the current month
    last_thursday = get_last_thursday(year, month)

    # If today is after the last Wednesday, calculate for the next month
    if today > last_thursday:
        if month == 12:  # Move to next year if December
            year += 1
            month = 1
        else:
            month += 1
        last_thursday = get_last_thursday(year, month)

    return last_thursday


def next_weekday(weekday_str):
    weekdays = {
        'mon': 0,
        'tue': 1,
        'wed': 2,
        'thu': 3,
        'fri': 4,
        'sat': 5,
        'sun': 6
    }

    today = datetime.today()
    target_weekday = weekdays.get(weekday_str.lower()) + 1
    days_ahead = (
                         target_weekday - today.weekday() + 7) % 7  # Calculate days until next Wednesday (Thursday is represented as 3)

    if days_ahead == 0:  # If today is Thursday, move to the next day
        days_ahead = 7

    next_expiry_day = today + timedelta(days=days_ahead)
    return next_expiry_day


def round_to_nearest(number, multiple):
    return round(number / multiple) * multiple


def get_next_thursday():
    # Get today's date
    today = datetime.today()

    # Find the next Thursday
    days_until_thursday = (3 - today.weekday()) % 7  # 3 represents Thursday (Monday=0)
    next_thursday = today + timedelta(days=days_until_thursday)
    return next_thursday


@click.command(name='fetch_exp_days')
def fetch_exp_days():
    angel_obj = angel.get_angel_obj()
    timeframe = '3m'
    [nse_interval, nse_max_days_per_interval, is_custom_interval] = angel.get_angel_timeframe_details(timeframe)

    symbol_file = urllib.request.urlopen(
        'https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json')
    symbols = json.load(symbol_file)
    tocken_df = pd.DataFrame.from_dict(symbols)
    # tocken_df['expiry'] = pd.to_datetime(tocken_df['expiry'], format='%d%b%Y', errors='coerce')
    tocken_df['expiry'] = pd.to_datetime(tocken_df['expiry'])
    tocken_df = tocken_df.astype({'strike': float})
    tocken_df['strike'] = tocken_df['strike'] / 100
    tocken_df.sort_values(by='strike', inplace=True)

    df_option_index_data = tocken_df.loc[tocken_df['instrumenttype'] == 'OPTIDX']
    df_option_index_data = df_option_index_data.loc[df_option_index_data['name'] == 'NIFTY']

    unique_expiry_dates = df_option_index_data['expiry'].dt.date.unique()
    unique_expiry_dates = sorted(unique_expiry_dates)
    print(unique_expiry_dates)


app.cli.add_command(fetch_option_token)
app.cli.add_command(update_near_token)
app.cli.add_command(reset_options)
app.cli.add_command(fetch_exp_days)
