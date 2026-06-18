import os
from dotenv import load_dotenv

load_dotenv()

key = os.getenv("KOSIS_API_KEY")
print(repr(key))
print("length:", len(key))
print("last_char:", repr(key[-1]))