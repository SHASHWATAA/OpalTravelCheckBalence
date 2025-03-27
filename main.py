import time
import re
from collections import OrderedDict
from datetime import datetime, timedelta
import requests
from lxml import html
from selenium import webdriver
from selenium.webdriver import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from tabulate import tabulate
from webdriver_manager.chrome import ChromeDriverManager

import credentials

# Constants / Configuration
CHROME_PROFILE_PATH = credentials.CHROME_PROFILE_PATH
LOGIN_URL = credentials.LOGIN_URL
USERNAME = credentials.USERNAME
PASSWORD = credentials.PASSWORD
CARD_NAME = credentials.CARD_NAME

USERNAME_XPATH = '//*[@id="usernameCrtl"]'
PASSWORD_XPATH = '//*[@id="passwordCtrl"]'
MY_CARD_XPATH = f'//*[@id="carousel-inner"]//div[contains(@aria-label, "card named {CARD_NAME}")]'
TRAVEL_DATA_XPATH = ('//*[@id="tni-opal-activity-tab-content-container"]/div/div/tni-page-frame/'
                     'div/div/tni-card-activities/div/div[2]')
TRAVEL_DATA_ACTIVITIES_XPATH = (
    "//div[@class='date']"
)


def initialize_driver() -> webdriver.Chrome:
    """Initializes and returns a Chrome WebDriver instance."""
    chrome_options = Options()
    chrome_options.add_argument(f"user-data-dir={CHROME_PROFILE_PATH}")
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--window-size=1920,1080")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver


def login(driver: webdriver.Chrome) -> None:
    """Logs into the website using the provided credentials.py."""
    driver.get(LOGIN_URL)
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.XPATH, PASSWORD_XPATH)))

    driver.find_element(By.XPATH, USERNAME_XPATH).send_keys(USERNAME)
    driver.find_element(By.XPATH, PASSWORD_XPATH).send_keys(PASSWORD)
    driver.find_element(By.XPATH, PASSWORD_XPATH).send_keys(Keys.RETURN)


def extract_balance_info(aria_label_text: str):
    """
    Extracts balance and pending amounts from the aria-label text.

    Returns:
        tuple: (balance, pending) as floats or None if not found.
    """
    balance_match = re.search(r'balance\s*\$([0-9.]+)', aria_label_text, re.IGNORECASE)
    pending_match = re.search(r'pending\s*\$([0-9.]+)', aria_label_text, re.IGNORECASE)
    balance = float(balance_match.group(1)) if balance_match else 0
    pending = float(pending_match.group(1)) if pending_match else 0
    return balance, pending


def get_balance(driver: webdriver.Chrome):
    """Clicks the card element and extracts the balance information."""
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.XPATH, MY_CARD_XPATH)))
    card_element = driver.find_element(By.XPATH, MY_CARD_XPATH)
    card_element.click()
    aria_label_text = card_element.get_attribute("aria-label")
    return extract_balance_info(aria_label_text)


def get_travel_data_html(driver: webdriver.Chrome) -> str:
    """
    Waits for and extracts the HTML content containing travel data.

    Returns:
        str: The outer HTML of the travel data container.
    """
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.XPATH, TRAVEL_DATA_XPATH)))
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.XPATH, TRAVEL_DATA_ACTIVITIES_XPATH)))
    time.sleep(2)
    element = driver.find_element(By.XPATH, TRAVEL_DATA_XPATH)
    return element.get_attribute("outerHTML")


def parse_travel_data(tree) -> OrderedDict:
    """
    Parses travel data from an HTML tree and returns an ordered dictionary
    with dates as keys (formatted as "%A %d %b %Y") and a list of activities as values.
    """
    travel_data = {}

    # Find all activity containers (each date section)
    date_containers = tree.xpath('//div[contains(@class, "activity-by-date-container")]')
    for date_container in date_containers:
        date_element = date_container.xpath('.//div[contains(@class, "activity-date")]/text()')
        if not date_element:
            continue
        date_str = date_element[0].strip()
        try:
            date_obj = datetime.strptime(date_str, "%A %d %b %Y")
        except ValueError:
            continue  # Skip unexpected date formats

        travel_data[date_obj] = []

        # Find all travel activities within the container
        activities = date_container.xpath('.//li[contains(@class, "ng-star-inserted")]')
        for activity in activities:
            # Extract time
            time_element = activity.xpath('.//div[@class="date"]/text()')
            time_str = time_element[0].strip() if time_element else "00:00"

            # Extract start and end points
            start_point_element = activity.xpath('.//span[contains(@class, "from")]/text()')
            end_point_element = activity.xpath('.//span[contains(@class, "to")]/text()')
            start_point = start_point_element[0].strip() if start_point_element else "Unknown"
            end_point = end_point_element[0].strip() if end_point_element else "Unknown"

            # Extract fare
            fare_element = activity.xpath('.//div[contains(@class, "amount")]/span/text()')
            fare = float(fare_element[0].strip().replace("$", "")) if fare_element else 0.0

            # Normalize top-up activities
            if "Top up" in start_point:
                start_point = "Top Up"
                end_point = "Opal Travel App"

            travel_data[date_obj].append({
                "time": time_str,
                "start_point": start_point,
                "end_point": end_point,
                "fare": fare
            })

        # Sort activities by time for the given date
        travel_data[date_obj].sort(key=lambda x: datetime.strptime(x["time"], "%H:%M"))

    # Return an OrderedDict sorted by date in descending order
    sorted_travel_data = OrderedDict(
        (date.strftime("%A %d %b %Y"), travel_data[date])
        for date in sorted(travel_data.keys(), reverse=True)
    )
    return sorted_travel_data


def calculate_totals(travel_dict: dict, last_monday: datetime):
    """
    Calculates the total top-up and fare charged since the given last Monday.

    Returns:
        tuple: (total_top_up, total_fare_charged)
    """
    total_top_up = 0.0
    total_fare_charged = 0.0
    for date_str, activities in travel_dict.items():
        date_obj = datetime.strptime(date_str, "%A %d %b %Y")
        if date_obj >= last_monday:
            for activity in activities:
                if activity["start_point"] == "Top Up":
                    total_top_up += activity["fare"]
                else:
                    total_fare_charged += activity["fare"]
    return total_top_up, total_fare_charged


def get_daily_totals(travel_dict: dict, last_monday: datetime) -> dict:
    """
    Aggregates top-up and fare charges per weekday from travel_dict entries
    on or after last_monday.

    For travel transactions (non–top-up), we take the absolute value of the fare.

    Returns:
        dict: A dictionary where keys are weekdays (e.g. "Monday") and values
              are dictionaries with keys "topup" and "fares".
    """
    daily_totals = {}
    for date_str, activities in travel_dict.items():
        # Convert the date string to a datetime object
        date_obj = datetime.strptime(date_str, "%A %d %b %Y")
        # Filter out dates before last Monday
        if date_obj < last_monday:
            continue

        weekday = date_obj.strftime("%A")
        if weekday not in daily_totals:
            daily_totals[weekday] = {"topup": 0.0, "fares": 0.0}
        for activity in activities:
            if activity["start_point"] == "Top Up":
                daily_totals[weekday]["topup"] += activity["fare"]
            else:
                # For travel fares, assume they are negative so we add the absolute value.
                daily_totals[weekday]["fares"] += abs(activity["fare"])
    return daily_totals


def build_table_string(daily_totals: dict) -> str:
    """
    Builds a pretty ASCII table using the 'tabulate' library.

    'daily_totals' is a dict like:
        {
            "Monday":    {"topup": 10.0, "fares": 15.2},
            "Tuesday":   {"topup":  5.0, "fares":  0.0},
            ...
        }

    Returns:
        str: A formatted ASCII table with columns for Weekday, Top Up, and Fares.
    """
    # Define the display order of weekdays
    weekdays_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    # Prepare rows for tabulate
    rows = []
    total_topup = 0.0
    total_fares = 0.0

    for day in weekdays_order:
        if day in daily_totals:
            topup = daily_totals[day]["topup"]
            fares = daily_totals[day]["fares"]
            rows.append([day, f"{topup:.2f}", f"{fares:.2f}"])
            total_topup += topup
            total_fares += fares

    # Add a final row for totals
    rows.append(["Total", f"{total_topup:.2f}", f"{total_fares:.2f}"])

    # Produce a nice table string
    table_str = tabulate(
        rows,
        headers=["Weekday", "Top Up", "Fares"],
        tablefmt="grid"  # or "github", "fancy_grid", etc.
    )
    return table_str


def main():
    driver = initialize_driver()
    try:
        login(driver)
        balance, pending = get_balance(driver)
        print(f"Balance: {float(balance):.2f}")
        print(f"Pending Top-Up: {float(pending):.2f}")

        travel_html = get_travel_data_html(driver)
        tree = html.fromstring(travel_html)
        travel_dict = parse_travel_data(tree)

        today = datetime.today()
        last_monday = today - timedelta(days=today.weekday())
        last_monday = last_monday.replace(hour=0, minute=0, second=0, microsecond=0)

        total_top_up, total_fare_charged = calculate_totals(travel_dict, last_monday)
        topup_needed = 50 -  (balance - total_fare_charged)

        if topup_needed < 0:
            topup_needed = 0


        print(f"Total topped up since last Monday: {float(total_top_up):.2f}")
        print(f"Total fare charged since last Monday: {float(total_fare_charged):.2f}")
        print(f"Total top up needed this week: {float(topup_needed):.2f}")



        # Compute daily totals for dates on or after last Monday
        daily_totals = get_daily_totals(travel_dict, last_monday)

        # Build the table string
        table_str = build_table_string(daily_totals)

        # Print locally if you want to see it
        print("\nTravel Activity Summary:")
        print(table_str)

        # --- SEND TO YOUR API ---
        url = credentials.url
        data = {
            "weekly_fare": total_fare_charged,
            "opal_balance": balance,
            "week_top_up": total_top_up,
            "top_up_needed": f"{float(topup_needed):.2f}",
        }

        # If your endpoint expects a POST with query params, you can do:
        response = requests.post(url, json=data)

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
