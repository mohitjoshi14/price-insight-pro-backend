#!/bin/bash

# Load environment variables from .env file
if [ -f .env ]; then
  echo "Loading .env file..."
  set -a
  source .env
  set +a
else
  echo "Warning: .env file not found, using defaults"
  BASE_URL="http://localhost:8000"
fi

echo "Using BASE_URL: $BASE_URL"
echo ""

echo "=== 1. Health Check ==="
curl -s $BASE_URL/health | jq . 2>/dev/null || echo "Error: Invalid JSON response"

# echo -e "\n=== 2. Suggest Competitors ==="
# RESPONSE=$(curl -s -X POST $BASE_URL/api/suggest-competitors \
#   -H "Content-Type: application/json" \
#   -d '{"listing_url": "https://www.airbnb.com/rooms/1563238432740070089"}')

echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"

# echo -e "\n=== 3. Submit Email ==="
# curl -s -X POST $BASE_URL/api/submit-email \
#   -H "Content-Type: application/json" \
#   -d '{
#     "email": "test@example.com",
#     "name": "Test User",
#     "listing_id": "51969750",
#     "subscribe_updates": false
#   }' | jq . 2>/dev/null || echo "Error: Invalid JSON response"

echo -e "\n=== 4. Track Prices (Small Test) ==="
echo "This may take 30-60 seconds..."
TRACK_RESPONSE=$(curl -s -X POST $BASE_URL/api/track-prices \
  -H "Content-Type: application/json" \
  -d '{
    "my_listing_id": "858697692672545141",
    "num_days": 5,
    "currency": "USD"
  }')

echo "$TRACK_RESPONSE" | jq . 2>/dev/null || {
  echo "Error parsing response. Raw output:"
  echo "$TRACK_RESPONSE"
}

echo -e "\n=== Done ==="
