#!/usr/bin/env python3
"""
Quick test to verify discord.py-self is installed correctly
"""

print("Testing Discord installation...")
print("="*60)

try:
    import discord
    print("✅ discord module found")
    print(f"   Version: {discord.__version__}")
    
    # discord.py-self 2.1.0 doesn't have Intents - that's OK!
    if hasattr(discord, 'Intents'):
        print("✅ discord.Intents exists (newer version)")
    else:
        print("ℹ️  discord.Intents not found (discord.py-self 2.1.0 - this is OK!)")
    
    if hasattr(discord, 'Client'):
        print("✅ discord.Client exists")
    else:
        print("❌ discord.Client NOT FOUND")
        input("\nPress Enter...")
        exit(1)
    
    # Try creating a Client object
    try:
        client = discord.Client()
        print("✅ discord.Client() works")
    except Exception as e:
        print(f"❌ discord.Client() failed: {e}")
        input("\nPress Enter...")
        exit(1)
    
    print("="*60)
    print("✅ ALL TESTS PASSED!")
    print("   discord.py-self 2.1.0 is installed correctly")
    print("="*60)
    print("\nYou can now run START.bat")
    
except ImportError as e:
    print("❌ discord module NOT installed")
    print(f"   Error: {e}")
    print("\n   Run: pip install discord.py-self")
    input("\nPress Enter...")
    exit(1)

except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
    input("\nPress Enter...")
    exit(1)

input("\nPress Enter to continue...")
