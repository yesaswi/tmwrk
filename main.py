import json
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright
import asyncio
import httpx
import time
from typing import Union
from fastapi.middleware.cors import CORSMiddleware


class LoginData(BaseModel):
    login_url: str
    login_code: str
    username: str
    password: str

class ShiftData(BaseModel):
    shift_start_date: str
    preferred_shift_groups: str
    shift_range: str


app = FastAPI()
# use CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
shift_checker_control = {"active": True}

LOGS = []


async def execute_playwright(login_url: str, login_code: str, username: str, password: str) -> Union[dict, None]:
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

            x_api_token = None

            # Function to capture the x-api-token
            def handle_request(request):
                nonlocal x_api_token
                for header, value in request.headers.items():
                    if header.lower() == 'x-api-token':
                        x_api_token = value
                        break

            # Add event listener for requests
            context.on("request", handle_request)

            page = await context.new_page()
            await page.goto(login_url)
            await page.wait_for_load_state('networkidle')
            await page.fill("input[name='code']", login_code)
            await page.fill("input[name='user']", username)
            await page.fill("input[name='pswd']", password)
            await page.press("input[name='pswd']", "Enter")
            await page.wait_for_load_state('networkidle')

            # Extract cookies and token
            cookies = await page.context.cookies()
            cookie_string = "; ".join([f"{cookie['name']}={cookie['value']}" for cookie in cookies])
            
            await browser.close()

            if x_api_token and cookie_string:
                return {"cookie": cookie_string, "x-api-token": x_api_token}
            else:
                return None
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def run_shift_checker(shift_start_date: str, preferred_shift_groups: str, cookie: str, x_api_token: str, shift_range: str = "week"):
    SWAPBOARD_API_URL = "https://tmwork.net/api/shift/swapboard"
    PARAMS = {'date': shift_start_date, 'range': shift_range}
    API_HEADERS = {'Cookie': cookie, 'x-api-token': x_api_token}
    preferred_shift_groups_str = preferred_shift_groups
    user_preferred_shift_groups = [group.strip() for group in preferred_shift_groups_str.split(',')]
    yield f"Shift checker started\n"
    yield f"Shift start date: {shift_start_date}\n"
    yield f"Shift range: {shift_range}\n"
    yield f"Checking for shifts in {user_preferred_shift_groups}\n"

    while shift_checker_control["active"]:
        try:
            await asyncio.sleep(5)
            async with httpx.AsyncClient() as client:
                try:
                    r = await client.get(url=SWAPBOARD_API_URL, params=PARAMS, headers=API_HEADERS)
                    now = time.strftime("%H:%M:%S", time.localtime())
                    yield f"{now} - {r.status_code} - {r.json()}\n"
                    if r.status_code == 400:
                        yield(f"Error {r.status_code}: {r.text}")
                        if r.text.find("(30) minutes") != -1:
                            LOGS.clear()
                            yield(f"Sleeping for 30 minutes.")
                            for i in range(6):
                                await asyncio.sleep(300)
                                yield(f"Slept {i * 1} / 30 minutes.")
                        continue
                    r.raise_for_status()
                except httpx.RequestError:
                    continue
                try:
                    data = r.json()
                    if len(data) == 0:
                        continue
                    elif len(data) > 0:
                        yield f"{now} - {data}\n"
                except Exception:
                    continue

                sorted_data = sorted(data, key=lambda i: i['SchId'], reverse=True)

                for item in sorted_data:
                    shift_id = item["SchId"]
                    ID = item["Id"]
                    shift_group = item["ShiftGroup"]
                    shift_date = item["Date"]

                    if shift_group in user_preferred_shift_groups and shift_date > shift_start_date:
                        POST_URL = "https://tmwork.net/api/shift/swap/claim"
                        POST_PARAMS = {'id': ID, 'bid': "3557", 'schid': shift_id}
                        try:
                            resp = await client.put(url=POST_URL, params=POST_PARAMS, headers=API_HEADERS)
                            if resp.text.find("Shift not found") != -1:
                                yield f"Shift ID: {shift_id} ID: {ID} Shift: {shift_group} Date: {shift_date} Claim failed\n"
                                continue
                        except httpx.RequestError:
                            continue
                        yield f"Shift ID: {shift_id} ID: {ID} Shift: {shift_group} Date: {shift_date} claimed\n"
                    else:
                        yield f"Shift ID: {shift_id} ID: {ID} Shift: {shift_group} Date: {shift_date} not in preferred group\n"
        except Exception as e:
            yield f"Error: {str(e)}\n"
            continue


async def consume_run_shift_checker(*args, **kwargs):
    async for item in run_shift_checker(*args, **kwargs):
        # Process each item yielded by run_shift_checker
        LOGS.append(f"{item}\n")  # Append with newline for readability

@app.post('/api/v1/playwright', status_code=status.HTTP_200_OK)
async def run_playwright(data: LoginData):
    result = await execute_playwright(data.login_url, data.login_code, data.username, data.password)
    # Check if result is a valid dictionary and contains necessary data
    if not isinstance(result, dict) or not all(result.get(key) for key in ["cookie", "x-api-token"]):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid or incomplete result from Playwright")
    # Save cookie and token to file
    try:
        # json dump to file
        json.dump(result, open("data.json", "w"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(content=result)

@app.post("/api/v1/start-shift-check", status_code=status.HTTP_200_OK)
async def start_shift_check(data: ShiftData):
    shift_checker_control["active"] = True
    # check data.json exists and contains cookie and token data and load
    try:
        with open("data.json", "r") as f:
            cookie, x_api_token = eval(f.read()).values()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    task = asyncio.create_task(consume_run_shift_checker(shift_start_date=data.shift_start_date, preferred_shift_groups=data.preferred_shift_groups, shift_range=data.shift_range, cookie=cookie, x_api_token=x_api_token))
    
    return JSONResponse(content={"message": "Running shift checker"})

@app.get("/api/v1/stop-shift-check", status_code=status.HTTP_200_OK)
async def stop_shift_check():
    shift_checker_control["active"] = False
    return JSONResponse(content={"message": "Shift check stopped"})

@app.get("/api/v1/logs", status_code=status.HTTP_200_OK)
async def get_logs():
    def log_generator():
        for log in LOGS:
            yield log
    return StreamingResponse(log_generator(), media_type="text/plain")