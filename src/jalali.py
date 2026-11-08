import datetime, re

MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
WEEKDAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]


def g2j(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053); days %= 12053
    jy += 4 * (days // 1461); days %= 1461
    if days > 365:
        jy += (days - 1) // 365; days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31; jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30; jd = 1 + (days - 186) % 30
    return jy, jm, jd


def j2g(jy, jm, jd):
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd + ((jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186)
    gy = 400 * (days // 146097); days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524); days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461); days %= 1461
    if days > 365:
        gy += (days - 1) // 365; days = (days - 1) % 365
    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0
    ml = [0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 13 and gd > ml[gm]:
        gd -= ml[gm]; gm += 1
    return gy, gm, gd


def parts(j):
    return tuple(int(p) for p in j.split("/"))


def to_date(j):
    return datetime.date(*j2g(*parts(j)))


def from_date(d):
    return "%04d/%02d/%02d" % g2j(d.year, d.month, d.day)


def today_j():
    return from_date(datetime.date.today())


def add_days(j, n):
    return from_date(to_date(j) + datetime.timedelta(days=n))


def diff_days(a, b):
    """b - a in days"""
    return (to_date(b) - to_date(a)).days


def month_len(jy, jm):
    if jm <= 6:
        return 31
    if jm <= 11:
        return 30
    return (to_date("%04d/01/01" % (jy + 1)) - to_date("%04d/12/01" % jy)).days


def weekday(j):  # 0 = شنبه
    return (to_date(j).weekday() + 2) % 7


def week_start(j):
    return add_days(j, -weekday(j))


def month_start(j):
    return j[:8] + "01"


def month_start_j():
    return month_start(today_j())


def year_start(j):
    return j[:5] + "01/01"


def shift_month(j, n):
    y, m, d = parts(j)
    m += n
    while m < 1:
        m += 12; y -= 1
    while m > 12:
        m -= 12; y += 1
    return "%04d/%02d/%02d" % (y, m, min(d, month_len(y, m)))


def shift_year(j, n):
    y, m, d = parts(j)
    y += n
    return "%04d/%02d/%02d" % (y, m, min(d, month_len(y, m)))


def month_end(j):
    y, m, _ = parts(j)
    return "%04d/%02d/%02d" % (y, m, month_len(y, m))


def month_name(j):
    y, m, _ = parts(j)
    return f"{MONTHS[m - 1]} {y}"


def norm_date(s):
    """1405/7/2 -> 1405/07/02 ; None if invalid"""
    if not s:
        return None
    s = s.strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩-", "01234567890123456789/"))
    m = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    if not (1300 <= y <= 1500) or not (1 <= mo <= 12) or not (1 <= d <= month_len(y, mo)):
        return None
    return "%04d/%02d/%02d" % (y, mo, d)
