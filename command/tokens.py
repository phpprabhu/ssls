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
        upcoming_expires = index_options['expiry'].drop_duplicates().sort_values().head(2).to_numpy()

        selected_expiry = upcoming_expires[0] if config.CURRENT_EXPIRY else upcoming_expires[1]
        index_options = index_options[index_options['expiry'] == selected_expiry]

        # Filter based on market direction
        if config.MARKET_DIRECTION == "UP":
            index_options = index_options[index_options['symbol'].str.endswith('PE')]
        elif config.MARKET_DIRECTION == "DOWN":
            index_options = index_options[index_options['symbol'].str.endswith('CE')]

        pd.set_option('display.max_rows', None)
        pd.set_option('display.max_columns', None)

        for i, option_data in index_options.iterrows():
            option_exists = Options.query.filter_by(instrument_token=option_data.token).first()

            if not option_exists:
                option = Options(symbol=option_data.symbol, name=option_data['name'],
                                 instrument_token=int(option_data.token),
                                 exchange_token=0,
                                 segment='NFO', instrument_type=option_data.symbol[-2] + option_data.symbol[-1],
                                 lot_size=option_data.lotsize, strike=float(option_data.strike),
                                 expiry=option_data.expiry, exchange=index.exchange)

                option.atm = False
                option.near = False
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
app.cli.add_command(reset_options)
app.cli.add_command(fetch_exp_days)
