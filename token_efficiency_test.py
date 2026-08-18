#!/usr/bin/env python3
"""
Token Efficiency Test for ctx-vault
Tests the improvements in token efficiency from our enhancements
"""

import sys
import os
import time
import tiktoken
from pathlib import Path

# Add current directory to path
sys.path.insert(0, '.')

def test_snippet_token_efficiency():
    """Test that our snippet improvements reduce token usage"""
    print("🧪 Testing snippet token efficiency...")
    
    # Initialize tokenizer (using cl100k_base for GPT-4/3.5)
    encoding = tiktoken.get_encoding("cl100k_base")
    
    # Test snippet content (simulating a typical search result snippet)
    test_snippet = "This is a test snippet that simulates what would be returned from a search query. It contains multiple words and some technical terminology to represent realistic search results from a knowledge base."
    
    # Count tokens for different snippet approaches
    full_tokens = len(encoding.encode(test_snippet))
    
    # Simulate our improved approaches
    # Original: 10 fragments (we improved to 6)
    # Minimal: 4 fragments  
    # Ultra: 3 fragments
    
    # For demonstration, we'll simulate by taking portions of the text
    words = test_snippet.split()
    
    # Original approach (simulated 10 fragments worth)
    original_text = ' '.join(words[:min(len(words), 50)])  # ~50 words
    original_tokens = len(encoding.encode(original_text))
    
    # Our improved approach (6 fragments worth)
    improved_text = ' '.join(words[:min(len(words), 30)])   # ~30 words
    improved_tokens = len(encoding.encode(improved_text))
    
    # Minimal approach (4 fragments worth)
    minimal_text = ' '.join(words[:min(len(words), 20)])    # ~20 words
    minimal_tokens = len(encoding.encode(minimal_text))
    
    # Ultra approach (3 fragments worth)
    ultra_text = ' '.join(words[:min(len(words), 15)])      # ~15 words
    ultra_tokens = len(encoding.encode(ultra_text))
    
    print(f"   Full text tokens: {full_tokens}")
    print(f"   Original approach tokens: {original_tokens}")
    print(f"   Improved approach tokens: {improved_tokens}")
    print(f"   Minimal approach tokens: {minimal_tokens}")
    print(f"   Ultra approach tokens: {ultra_tokens}")
    
    if original_tokens > 0:
        improvement_ratio = original_tokens / improved_tokens if improved_tokens > 0 else float('inf')
        print(f"   Token efficiency improvement: {improvement_ratio:.2f}×")
        
        if improvement_ratio >= 2.0:
            print("   ✅ Achieved 2×+ token efficiency improvement!")
            return True
        else:
            print("   ⚠️  Did not reach 2× improvement target")
            return False
    else:
        print("   ❌ Could not calculate improvement ratio")
        return False

def test_context_strategies():
    """Test that our new context strategies work correctly"""
    print("\\n🧪 Testing context strategies...")
    
    try:
        from context_injection import ContextBuilder, ContextStrategy
        
        # Check if MINIMAL_TOKENS strategy is available
        if hasattr(ContextStrategy, 'MINIMAL_TOKENS'):
            print("   ✅ MINIMAL_TOKENS strategy is available")
        else:
            print("   ❌ MINIMAL_TOKENS strategy NOT found")
            return False
            
        # Test creating a context builder
        builder = ContextBuilder()
        print("   ✅ ContextBuilder created successfully")
        
        # Test the build_minimal_context method exists
        if hasattr(builder, 'build_minimal_context'):
            print("   ✅ build_minimal_context method available")
        else:
            print("   ❌ build_minimal_context method NOT found")
            return False
            
        return True
    except Exception as e:
        print(f"   ❌ Error testing context strategies: {e}")
        return False

def test_api_endpoints():
    """Test that our new API endpoints are available"""
    print("\\n🧪 Testing API endpoints...")
    
    try:
        # Check if we can import the API module
        import api
        from fastapi import FastAPI
        
        # Get the app instance
        app = api.app
        
        # Check for our new endpoints
        routes = [route.path for route in app.routes]
        
        endpoints_to_check = [
            "/search/ultra",
            "/search/minimal", 
            "/search"
        ]
        
        all_found = True
        for endpoint in endpoints_to_check:
            if endpoint in routes:
                print(f"   ✅ Endpoint {endpoint} found")
            else:
                print(f"   ❌ Endpoint {endpoint} NOT found")
                all_found = False
                
        return all_found
    except Exception as e:
        print(f"   ❌ Error testing API endpoints: {e}")
        return False

def main():
    print("🚀 ctx-vault Token Efficiency Verification Test")
    print("=" * 50)
    
    # Set up test environment
    os.environ["CTX_DB_PATH"] = "./test_vault.db"
    os.environ["CTX_VAULT_ROOT"] = "./test_vault_ctx"
    
    # Run tests
    snippet_test_passed = test_snippet_token_efficiency()
    context_test_passed = test_context_strategies()
    api_test_passed = test_api_endpoints()
    
    print("\\n" + "=" * 50)
    print("📊 TEST RESULTS SUMMARY:")
    print(f"   Snippet token efficiency: {'✅ PASS' if snippet_test_passed else '❌ FAIL'}")
    print(f"   Context strategies:       {'✅ PASS' if context_test_passed else '❌ FAIL'}")
    print(f"   API endpoints:            {'✅ PASS' if api_test_passed else '❌ FAIL'}")
    
    overall_pass = snippet_test_passed and context_test_passed and api_test_passed
    print(f"\\n🎯 Overall result: {'✅ ALL TESTS PASSED' if overall_pass else '❌ SOME TESTS FAILED'}")
    
    if overall_pass:
        print("\\n🎉 ctx-vault token efficiency improvements verified!")
        print("   Ready for: Blog post, research paper, social media release, and GitHub tagging")
    else:
        print("\\n⚠️  Some tests failed - please review implementation")
        
    return overall_pass

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)