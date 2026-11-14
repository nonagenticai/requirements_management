#!/bin/bash
# Diagnostic script for requirements-api health issues

echo "=== Requirements API Health Diagnostics ==="
echo ""

echo "1. Container Status:"
docker ps --filter "name=requirements-api" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo ""

echo "2. Recent Container Logs (last 50 lines):"
docker logs requirements-api --tail=50
echo ""

echo "3. Test Health Endpoint (from inside container):"
docker exec requirements-api curl -f http://localhost:8921/health 2>&1 || echo "Health check failed!"
echo ""

echo "4. Check if app is listening on port 8921:"
docker exec requirements-api netstat -tlnp 2>/dev/null | grep 8921 || echo "App not listening on 8921"
echo ""

echo "5. Test Database Connectivity:"
docker exec requirements-api pg_isready -h pgbouncer.example.com -p 6432 -U requirements_user 2>&1 || echo "Database not reachable"
echo ""

echo "6. Check Certificate Files:"
docker exec requirements-api ls -la /app/certs/ 2>&1 || echo "Cannot access certs directory"
echo ""

echo "7. Test DNS Resolution:"
docker exec requirements-api getent hosts pgbouncer.example.com 2>&1 || echo "Cannot resolve pgbouncer.example.com"
echo ""

echo "8. Check Python Process:"
docker exec requirements-api ps aux | grep uvicorn || echo "Uvicorn not running"
echo ""

echo "=== Diagnostics Complete ==="
