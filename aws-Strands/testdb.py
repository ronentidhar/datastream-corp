import sqlite3
import boto3
from strands import Agent

def test_database():
    try:
        conn = sqlite3.connect('datastream_corp.db')
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM employees")
        count = cursor.fetchone()[0]
        print(f"✓ Database connected: {count} employees found")
        cursor.execute("SELECT name FROM departments")
        departments = [row[0] for row in cursor.fetchall()]
        print(f"✓ Departments: {', '.join(departments)}")
        conn.close()
        return True
    except Exception as e:
        print(f"✗ Database error: {e}")
        return False

def test_aws_credentials():
    try:
        sts = boto3.client('sts')
        identity = sts.get_caller_identity()
        print(f"✓ AWS configured: {identity.get('Arn', 'Unknown')}")
        return True
    except Exception as e:
        print(f"✗ AWS error: {e}")
        return False

def test_strands_import():
    try:
        agent = Agent(system_prompt="Test agent")
        print("✓ Strands SDK imported successfully")
        return True
    except Exception as e:
        print(f"✗ Strands error: {e}")
        return False

if __name__ == "__main__":
    print("=" * 50)
    print("  DataStream Bootcamp Setup Verification")
    print("=" * 50)
    print()
    
    tests = [
        ("Database Connection", test_database),
        ("AWS Credentials", test_aws_credentials),
        ("Strands SDK", test_strands_import)
    ]
    
    results = []
    for i, (name, test_func) in enumerate(tests, 1):
        print(f"[{i}/3] Testing {name}...")
        results.append(test_func())
        print()
    
    print("=" * 50)
    if all(results):
        print("🎉 SETUP COMPLETE! You're ready for Task 1.")
    else:
        print("⚠️  Some tests failed. Please review the steps above.")
    print("=" * 50)
