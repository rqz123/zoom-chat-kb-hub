import uvicorn


if __name__ == "__main__":
    uvicorn.run("zoom_kb.app:app", host="127.0.0.1", port=8765, reload=False)

