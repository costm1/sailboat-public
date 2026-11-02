"""Owner-selected regular equity session. Verified NYSE calendar, no network guessing.
Source: https://www.nyse.com/trade/hours-calendars, checked 2026-10-23.
Unverified years fail closed. Exceptional unscheduled closures need an owner update.
"""
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
EASTERN=ZoneInfo('America/New_York')
HOLIDAYS={
 2026: {'01-01','01-19','02-16','04-03','05-25','06-19','07-03','09-07','11-26','12-25'},
 2027: {'01-01','01-18','02-15','03-26','05-31','06-18','07-05','09-06','11-25','12-24'},
 2028: {'01-17','02-21','04-14','05-29','06-19','07-04','09-04','11-23','12-25'},
}
EARLY={'2026-11-27','2026-12-24','2027-11-26','2028-07-03','2028-11-24'}
class RegularMarketHours:
    name='NYSE_REGULAR'
    def session(self,day):
        if day.year not in HOLIDAYS: return None
        if day.weekday()>=5 or day.strftime('%m-%d') in HOLIDAYS[day.year]: return None
        opening=datetime.combine(day,time(9,30),EASTERN).timestamp()
        closing=datetime.combine(day,time(13 if day.isoformat() in EARLY else 16),EASTERN).timestamp()
        return opening,closing
    def status(self,now):
        day=datetime.fromtimestamp(now,EASTERN).date()
        base={'calendar':self.name,'timezone':'America/New_York','verified_through':'2028-12-31'}
        if day.year not in HOLIDAYS:
            return base|{'open':False,'reason':'CALENDAR_UNVERIFIED','next_open':None,'close':None}
        today=self.session(day)
        if today and today[0]<=now<today[1]:
            return base|{'open':True,'reason':'REGULAR_SESSION','next_open':today[0],'close':today[1]}
        for i in range(370):
            session=self.session(day+timedelta(days=i))
            if session and session[0]>now:
                return base|{'open':False,'reason':'MARKET_CLOSED','next_open':session[0],'close':session[1]}
        return base|{'open':False,'reason':'CALENDAR_UNVERIFIED','next_open':None,'close':None}
