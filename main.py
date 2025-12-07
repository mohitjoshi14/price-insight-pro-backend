"""
Airbnb Price Tracking API
Stack: FastAPI, Playwright async_api, Pydantic, Redis cache, PostgreSQL
Run: uvicorn main:app --host 0.0.0.0 --port 8000
"""

import json
import re
import time
import random
import asyncio
import os
from datetime import datetime, timedelta
from typing import List, Optional
from contextlib import asynccontextmanager
from urllib.parse import urlparse, unquote

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# Database imports
import redis
import psycopg2
from psycopg2.extras import RealDictCursor

# ==================== CONFIGURATION ====================

CACHE_EXPIRY_HOURS = 12
PLAYWRIGHT_TIMEOUT = 45000  # 45 seconds
SUPPORTED_CURRENCIES = ["INR", "USD", "EUR", "GBP", "AUD", "CAD", "SGD", "AED"]
MAX_COMPETITORS = 3
MAX_DAYS = 7
STAY_LENGTH_NIGHTS = 2  # Number of nights per stay
REQUEST_DELAY = (1, 2)
AIRBNB_BASE_URL = "https://www.airbnb.com"

# Railway Configuration
BROWSERLESS_ENDPOINT = os.environ.get('BROWSER_PLAYWRIGHT_ENDPOINT_PRIVATE', '')
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
DATABASE_URL = os.environ.get('DATABASE_URL', '')

# Global connections
redis_client = None
db_conn = None

# ==================== PYDANTIC MODELS ====================

class SuggestCompetitorsRequest(BaseModel):
    listing_url: str

    @field_validator("listing_url")
    @classmethod
    def validate_url(cls, v):
        if "airbnb" not in v.lower() or "/rooms/" not in v:
            raise ValueError("Invalid Airbnb listing URL")
        return v

class DetectedCompetitor(BaseModel):
    listing_id: str
    listing_url: str
    listing_name: str
    thumbnail: Optional[str] = None

class SuggestCompetitorsResponse(BaseModel):
    competitors: List[DetectedCompetitor]
    count: int

class TrackPricesRequest(BaseModel):
    my_listing_id: str
    competitor_listing_ids: List[str] = Field(default_factory=list)
    num_days: int = Field(default=7, le=MAX_DAYS)
    currency: str = Field(default="USD")

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v):
        if v.upper() not in SUPPORTED_CURRENCIES:
            raise ValueError(f"Currency must be one of: {SUPPORTED_CURRENCIES}")
        return v.upper()

    @field_validator("competitor_listing_ids")
    @classmethod
    def validate_competitors(cls, v):
        if len(v) > MAX_COMPETITORS:
            raise ValueError(f"Maximum {MAX_COMPETITORS} competitors allowed")
        return v

class SubmitEmailRequest(BaseModel):
    email: str
    name: str
    listing_id: str
    subscribe_updates: bool = False

# ==================== DATABASE SETUP ====================

def init_postgres():
    """Initialize PostgreSQL connection and create leads table."""
    global db_conn
    try:
        db_conn = psycopg2.connect(DATABASE_URL)
        cursor = db_conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) NOT NULL,
                name VARCHAR(255) NOT NULL,
                listing_id VARCHAR(50) NOT NULL,
                subscribe_updates BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db_conn.commit()
        cursor.close()
        print("✅ PostgreSQL connected and table initialized")
    except Exception as e:
        print(f"⚠️  PostgreSQL connection failed: {e}")
        db_conn = None

def init_redis():
    """Initialize Redis connection."""
    global redis_client
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        print("✅ Redis connected")
    except Exception as e:
        print(f"⚠️  Redis connection failed: {e}")
        redis_client = None

# ==================== HELPER FUNCTIONS ====================

def extract_listing_id(url: str) -> str:
    """Extract listing ID from Airbnb URL."""
    match = re.search(r'/rooms/(\d+)', url)
    if match:
        return match.group(1)
    raise ValueError(f"Could not extract listing ID from URL: {url}")

def build_listing_url(listing_id: str) -> str:
    """Build full Airbnb URL from listing ID."""
    return f"{AIRBNB_BASE_URL}/rooms/{listing_id}"

def normalize_airbnb_url(url: str) -> str:
    """Normalize Airbnb URL to use .com domain."""
    return url.replace("airbnb.co.in", "airbnb.com").replace("airbnb.co.uk", "airbnb.com")

# ==================== CACHE UTILITIES (REDIS) ====================

def get_cache_key(listing_id: str, check_in: str, currency: str) -> str:
    return f"price:{listing_id}:{check_in}:{currency}"

def get_cached_price(listing_id: str, check_in: str, currency: str) -> Optional[int]:
    """Get price from Redis cache."""
    if not redis_client:
        return None
    
    try:
        cache_key = get_cache_key(listing_id, check_in, currency)
        cached_data = redis_client.get(cache_key)
        if cached_data:
            return int(cached_data)
    except Exception as e:
        print(f"Redis get error: {e}")
    
    return None

def set_cached_price(listing_id: str, check_in: str, currency: str, price: int) -> None:
    """Set price in Redis cache with expiry."""
    if not redis_client:
        return
    
    try:
        cache_key = get_cache_key(listing_id, check_in, currency)
        expiry_seconds = CACHE_EXPIRY_HOURS * 3600
        redis_client.setex(cache_key, expiry_seconds, price)
    except Exception as e:
        print(f"Redis set error: {e}")

# ==================== ASYNC SCRAPING FUNCTIONS ====================

async def fetch_price(listing_id: str, check_in: str, check_out: str, currency: str) -> tuple[Optional[int], bool]:
    """
    Fetch price from Airbnb listing. Returns (price, cached) or (None, False) on failure.
    """
    # Check cache first
    cached_price = get_cached_price(listing_id, check_in, currency)
    if cached_price is not None:
        return cached_price, True

    url = f"{AIRBNB_BASE_URL}/rooms/{listing_id}?adults=2&check_in={check_in}&check_out={check_out}&currency={currency}"

    price = None
    async with async_playwright() as p:
        # Connect to Railway Browserless instead of launching locally
        if BROWSERLESS_ENDPOINT:
            browser = await p.chromium.connect(BROWSERLESS_ENDPOINT)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        else:
            # Fallback for local development
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        
        page = await context.new_page()

        try:
            await page.goto(url, timeout=PLAYWRIGHT_TIMEOUT, wait_until="load")
            await asyncio.sleep(random.uniform(*REQUEST_DELAY))

            price_selectors = [
                "._1k1ce2w",
                "[data-testid='price-item-total']",
                "span._tyxjp1",
                "._1qs94rc span",
                "span._1y74zjx",
            ]

            for selector in price_selectors:
                try:
                    price_element = await page.query_selector(selector)
                    if price_element:
                        price_text = await price_element.inner_text()
                        numbers = re.findall(r'[\d,]+', price_text)
                        if numbers:
                            price = int(numbers[0].replace(",", ""))
                            break
                except Exception:
                    continue

        except Exception as e:
            print(f"Error scraping price for {listing_id} on {check_in}: {e}")
        finally:
            await page.close()
            await context.close()
            await browser.close()

    if price is not None:
        set_cached_price(listing_id, check_in, currency, price)

    return price, False

async def fetch_listing_name(listing_id: str) -> str:
    """Scrape h1 tag from listing page."""
    url = f"{AIRBNB_BASE_URL}/rooms/{listing_id}"
    name = f"Property {listing_id}"

    async with async_playwright() as p:
        # Connect to Railway Browserless
        if BROWSERLESS_ENDPOINT:
            browser = await p.chromium.connect(BROWSERLESS_ENDPOINT)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        else:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        
        page = await context.new_page()

        try:
            await page.goto(url, timeout=PLAYWRIGHT_TIMEOUT, wait_until="load")
            await asyncio.sleep(1)

            h1_element = await page.query_selector("h1")
            if h1_element:
                text = await h1_element.inner_text()
                if text.strip():
                    name = text.strip()

        except Exception as e:
            print(f"Error fetching name for {listing_id}: {e}")
        finally:
            await page.close()
            await context.close()
            await browser.close()

    return name

async def extract_listing_name_from_card(card_element, listing_id: str) -> str:
    """Extract listing name from a search result card element."""
    
    # Try multiple strategies to get the listing name
    title_selectors = [
        "[data-testid='listing-card-title']",
        "[data-testid='listing-card-name']",
        "div[id*='title']",
        ".t1jojoys",  # Common Airbnb title class
        "div[role='img'] + div",  # Title often follows the image
    ]

    # Strategy 1: Look for title in specific data attributes
    for selector in title_selectors:
        try:
            title_element = await card_element.query_selector(selector)
            if title_element:
                title_text = await title_element.inner_text()
                if title_text and len(title_text.strip()) > 0:
                    # Clean up the title
                    title = title_text.strip()
                    # Remove common prefixes that aren't part of the actual title
                    if not title.startswith("Property"):
                        return title[:100]
        except Exception:
            continue

    # Strategy 2: Look for any div with specific text patterns (property descriptions)
    try:
        # Find all text content in the card
        all_divs = await card_element.query_selector_all("div")
        for div in all_divs[:10]:  # Check first 10 divs only for performance
            try:
                text = await div.inner_text()
                text = text.strip()
                # Look for text that looks like a title (not too short, not a price, not a location)
                if (text and 
                    len(text) > 10 and 
                    len(text) < 100 and
                    not re.match(r'^[₹$€£¥]', text) and  # Not a price
                    not re.match(r'^\d+\s*(guest|bed|bath)', text, re.IGNORECASE) and  # Not capacity info
                    not re.match(r'^\d+\.\d+\s*★', text)):  # Not a rating
                    return text
            except Exception:
                continue
    except Exception:
        pass

    # Strategy 3: Get aria-label from the link itself
    try:
        link = await card_element.query_selector("a[href*='/rooms/']")
        if link:
            aria = await link.get_attribute("aria-label")
            if aria and len(aria) > 5:
                return aria[:100]
    except Exception:
        pass

    # Fallback
    return f"Nearby Property {listing_id}"

async def auto_find_competitors_async(listing_url: str, max_results: int = 5) -> List[DetectedCompetitor]:
    """Find competitor listings with proper name extraction."""
    competitors = []
    listing_url = normalize_airbnb_url(listing_url)

    try:
        listing_id = extract_listing_id(listing_url)
    except ValueError as e:
        print(f"ERROR: {e}")
        return []

    print(f"\n{'='*60}")
    print(f"COMPETITOR SEARCH")
    print(f"{'='*60}")
    print(f"Target Listing: {listing_id}")

    async with async_playwright() as p:
        # Connect to Railway Browserless
        if BROWSERLESS_ENDPOINT:
            browser = await p.chromium.connect(BROWSERLESS_ENDPOINT)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        else:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        
        page = await context.new_page()

        try:
            # Load listing page to get location
            await page.goto(listing_url, timeout=PLAYWRIGHT_TIMEOUT, wait_until="load")
            await asyncio.sleep(5)

            location = None

            # Try meta description
            try:
                meta_desc = await page.get_attribute('meta[property="og:description"]', 'content')
                if meta_desc:
                    match = re.search(r'in ([^-]+)', meta_desc)
                    if match:
                        location = match.group(1).strip()
            except:
                pass

            # Try breadcrumbs
            if not location:
                breadcrumbs = await page.query_selector_all("nav a, ol[role='list'] a")
                if len(breadcrumbs) >= 2:
                    try:
                        loc_text = await breadcrumbs[-1].inner_text()
                        if loc_text and len(loc_text) > 2:
                            location = loc_text.strip()
                    except:
                        pass

            if not location:
                print("⚠ Could not extract location")
                location = "nearby-stays"
            else:
                print(f"✓ Location: {location}")

            # Search for competitors
            search_url = f"{AIRBNB_BASE_URL}/s/{location.replace(' ', '-').replace(',', '')}/homes"
            print(f"Searching: {search_url}")

            await page.goto(search_url, timeout=PLAYWRIGHT_TIMEOUT, wait_until="load")
            await asyncio.sleep(4)

            # Find all listing cards
            card_containers = await page.query_selector_all(
                "[itemprop='itemListElement'], [data-testid='card-container'], div[data-testid='listing-card']"
            )

            if not card_containers:
                all_links = await page.query_selector_all("a[href*='/rooms/']")
                print(f"Found {len(all_links)} listing links")
            else:
                print(f"Found {len(card_containers)} listing cards")

            seen_ids = set()
            seen_ids.add(listing_id)

            if card_containers:
                for card in card_containers:
                    if len(competitors) >= max_results:
                        break

                    try:
                        link = await card.query_selector("a[href*='/rooms/']")
                        if not link:
                            continue

                        href = await link.get_attribute("href")
                        if not href:
                            continue

                        match = re.search(r'/rooms/(\d+)', href)
                        if not match:
                            continue

                        comp_id = match.group(1)
                        if comp_id in seen_ids:
                            continue

                        seen_ids.add(comp_id)

                        name = await extract_listing_name_from_card(card, comp_id)

                        thumbnail = None
                        try:
                            img = await card.query_selector("img")
                            if img:
                                thumbnail = await img.get_attribute("src")
                        except:
                            pass

                        competitors.append(DetectedCompetitor(
                            listing_id=comp_id,
                            listing_url=build_listing_url(comp_id),
                            listing_name=name,
                            thumbnail=thumbnail
                        ))

                        print(f"✓ {comp_id}: {name[:60]}")

                    except Exception as e:
                        print(f"  Error: {e}")
                        continue

            print(f"\nTotal found: {len(competitors)}")

        except Exception as e:
            print(f"\nERROR: {e}")
        finally:
            await page.close()
            await context.close()
            await browser.close()

    return competitors

# ==================== FASTAPI APP ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Airbnb Price Tracking API starting...")
    if BROWSERLESS_ENDPOINT:
        print(f"✅ Connected to Browserless: {BROWSERLESS_ENDPOINT[:50]}...")
    else:
        print("⚠️  Running in local mode (no Browserless)")
    
    # Initialize databases
    init_redis()
    init_postgres()
    
    yield
    
    # Cleanup
    if redis_client:
        redis_client.close()
    if db_conn:
        db_conn.close()
    print("👋 Shutting down...")

app = FastAPI(
    title="Airbnb Price Tracking API",
    description="Track and compare Airbnb listing prices",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== ENDPOINTS ====================

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "browserless_connected": bool(BROWSERLESS_ENDPOINT),
        "redis_connected": redis_client is not None,
        "postgres_connected": db_conn is not None,
        "timestamp": datetime.now().isoformat()
    }

@app.delete("/api/clear-cache")
async def clear_cache():
    """Clear all cached prices from Redis."""
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis not available")
    
    try:
        # Delete all keys matching price:*
        for key in redis_client.scan_iter("price:*"):
            redis_client.delete(key)
        return {"status": "success", "message": "Cache cleared"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/suggest-competitors", response_model=SuggestCompetitorsResponse)
async def suggest_competitors(request: SuggestCompetitorsRequest):
    try:
        competitors = await auto_find_competitors_async(request.listing_url)
        return SuggestCompetitorsResponse(
            competitors=competitors,
            count=len(competitors)
        )
    except Exception as e:
        print(f"Error in suggest_competitors: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/track-prices")
async def track_prices(request: TrackPricesRequest):
    today = datetime.now().date()
    dates = [(today + timedelta(days=i)).isoformat() for i in range(request.num_days)]

    async def get_listing_prices(lid: str) -> dict:
        name = await fetch_listing_name(lid)
        prices = []

        for i, check_in in enumerate(dates):
            check_out = (today + timedelta(days=i+STAY_LENGTH_NIGHTS)).isoformat()
            price, cached = await fetch_price(lid, check_in, check_out, request.currency)

            prices.append({
                "date": check_in,
                "price": price,
                "currency": request.currency
            })

        valid_prices = [p["price"] for p in prices if p["price"] is not None]
        avg = round(sum(valid_prices) / len(valid_prices), 2) if valid_prices else 0

        return {
            "listing_id": lid,
            "listing_url": build_listing_url(lid),
            "listing_name": name,
            "prices": prices,
            "average_price": avg,
            "min_price": min(valid_prices) if valid_prices else 0,
            "max_price": max(valid_prices) if valid_prices else 0
        }

    my_property_task = get_listing_prices(request.my_listing_id)
    competitor_tasks = [get_listing_prices(cid) for cid in request.competitor_listing_ids]

    results = await asyncio.gather(my_property_task, *competitor_tasks)
    my_property = results[0]
    competitors_data = results[1:]

    all_competitor_avgs = [c["average_price"] for c in competitors_data if c["average_price"] > 0]
    market_average = round(sum(all_competitor_avgs) / len(all_competitor_avgs), 2) if all_competitor_avgs else 0

    price_diff = 0
    if market_average > 0:
        price_diff = round(((my_property["average_price"] - market_average) / market_average) * 100, 2)

    return {
        "user_listing": my_property,
        "competitors": competitors_data,
        "market_average": market_average,
        "price_difference_percent": price_diff,
        "currency": request.currency,
        "tracking_period": request.num_days,
        "generated_at": datetime.now().isoformat() + "Z"
    }

@app.get("/api/track-prices-stream")
@app.post("/api/track-prices-stream")
async def track_prices_stream_endpoint(
    request: Request,
    user_listing_url: Optional[str] = Query(None),
    competitor_urls: Optional[List[str]] = Query(None),
    currency: Optional[str] = Query("USD"),
    tracking_days: Optional[int] = Query(7),
    body_request: Optional[TrackPricesRequest] = None
):
    """Unified SSE endpoint supporting GET and POST."""

    my_listing_id = ""
    competitor_ids = []
    curr = "USD"
    days = 7

    if request.method == "POST":
        if body_request:
            my_listing_id = body_request.my_listing_id
            competitor_ids = body_request.competitor_listing_ids
            curr = body_request.currency
            days = body_request.num_days
        else:
            try:
                data = await request.json()
                my_listing_id = data.get("my_listing_id")
                competitor_ids = data.get("competitor_listing_ids", [])
                curr = data.get("currency", "USD")
                days = data.get("num_days", 7)
            except:
                pass
    else:  # GET
        if not user_listing_url:
            return StreamingResponse(
                iter([f"data: {json.dumps({'type': 'error', 'message': 'Missing user_listing_url'})}\n\n"]),
                media_type="text/event-stream"
            )

        try:
            decoded_url = unquote(str(user_listing_url))
            my_listing_id = extract_listing_id(decoded_url)

            if competitor_urls:
                for c_url in competitor_urls:
                    try:
                        c_id = extract_listing_id(unquote(str(c_url)))
                        competitor_ids.append(c_id)
                    except:
                        continue

            curr = currency or "USD"
            days = tracking_days or 7

        except Exception as e:
            return StreamingResponse(
                iter([f"data: {json.dumps({'type': 'error', 'message': f'Invalid input: {str(e)}'})}\n\n"]),
                media_type="text/event-stream"
            )

    async def generate():
        try:
            today = datetime.now().date()
            date_range = [(today + timedelta(days=i)).isoformat() for i in range(days)]

            yield f"data: {json.dumps({'type': 'started', 'message': 'Price tracking initiated.'})}\n\n"

            my_name = await fetch_listing_name(my_listing_id)
            my_prices = []

            for i, check_in in enumerate(date_range):
                check_out = (today + timedelta(days=i+STAY_LENGTH_NIGHTS)).isoformat()
                price, cached = await fetch_price(my_listing_id, check_in, check_out, curr)
                my_prices.append({"date": check_in, "price": price, "currency": curr})

            valid_prices = [p["price"] for p in my_prices if p["price"] is not None]
            my_property = {
                "listing_id": my_listing_id,
                "listing_url": build_listing_url(my_listing_id),
                "listing_name": my_name,
                "prices": my_prices,
                "average_price": round(sum(valid_prices) / len(valid_prices), 2) if valid_prices else 0,
                "min_price": min(valid_prices) if valid_prices else 0,
                "max_price": max(valid_prices) if valid_prices else 0
            }

            yield f"data: {json.dumps({'type': 'my_property_complete', 'data': my_property})}\n\n"

            competitors_data = []
            for comp_id in competitor_ids:
                comp_name = await fetch_listing_name(comp_id)
                comp_prices = []

                for i, check_in in enumerate(date_range):
                    check_out = (today + timedelta(days=i+STAY_LENGTH_NIGHTS)).isoformat()
                    price, cached = await fetch_price(comp_id, check_in, check_out, curr)
                    comp_prices.append({"date": check_in, "price": price, "currency": curr})

                valid_prices = [p["price"] for p in comp_prices if p["price"] is not None]
                comp_data = {
                    "listing_id": comp_id,
                    "listing_url": build_listing_url(comp_id),
                    "listing_name": comp_name,
                    "prices": comp_prices,
                    "average_price": round(sum(valid_prices) / len(valid_prices), 2) if valid_prices else 0,
                    "min_price": min(valid_prices) if valid_prices else 0,
                    "max_price": max(valid_prices) if valid_prices else 0
                }
                competitors_data.append(comp_data)

                yield f"data: {json.dumps({'type': 'competitor_complete', 'data': comp_data})}\n\n"

            all_avgs = [c["average_price"] for c in competitors_data if c["average_price"] > 0]
            market_avg = round(sum(all_avgs) / len(all_avgs), 2) if all_avgs else 0
            diff_percent = round(((my_property["average_price"] - market_avg) / market_avg) * 100, 2) if market_avg > 0 else 0

            final_report = {
                "type": "complete",
                "report": {
                    "user_listing": my_property,
                    "competitors": competitors_data,
                    "market_average": market_avg,
                    "price_difference_percent": diff_percent,
                    "currency": curr,
                    "tracking_period": days,
                    "generated_at": datetime.now().isoformat() + "Z"
                }
            }
            yield f"data: {json.dumps(final_report)}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

@app.post("/api/submit-email")
async def submit_email(request: SubmitEmailRequest):
    """Store email lead in PostgreSQL database."""
    if not db_conn:
        raise HTTPException(status_code=503, detail="Database not available")
    
    try:
        cursor = db_conn.cursor()
        cursor.execute("""
            INSERT INTO leads (email, name, listing_id, subscribe_updates)
            VALUES (%s, %s, %s, %s)
        """, (request.email, request.name, request.listing_id, request.subscribe_updates))
        db_conn.commit()
        cursor.close()
        return {"status": "success", "message": "Email submitted successfully"}
    except Exception as e:
        db_conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.get("/api/leads")
async def get_leads():
    """Get all leads from database (admin endpoint)."""
    if not db_conn:
        raise HTTPException(status_code=503, detail="Database not available")
    
    try:
        cursor = db_conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM leads ORDER BY created_at DESC")
        leads = cursor.fetchall()
        cursor.close()
        return {"leads": leads, "count": len(leads)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
