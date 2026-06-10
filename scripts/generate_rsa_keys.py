#!/usr/bin/env python3
"""
Generate RSA key pairs for all agents in sample_agents.json

This script:
1. Reads the existing sample_agents.json file
2. Generates RSA public/private key pairs for each agent
3. Adds the keys to each agent's entry
4. Saves the updated JSON file

Usage:
    python scripts/generate_rsa_keys.py
"""

import json
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

# Project root directory
PROJECT_ROOT = Path(__file__).resolve().parents[1]

def generate_rsa_key_pair():
    """Generate an RSA public/private key pair"""
    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,  # Standard public exponent
        key_size=2048,          # 2048-bit key size for good security
    )
    
    # Get public key
    public_key = private_key.public_key()
    
    # Serialize private key to PEM format
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode('utf-8')
    
    # Serialize public key to PEM format
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode('utf-8')
    
    return private_pem, public_pem

def main():
    """Main function to generate RSA keys for all agents"""
    
    # Load existing agents file
    agents_file = PROJECT_ROOT / "data" / "sample_agents.json"
    
    print(f"Loading agents from: {agents_file}")
    with open(agents_file, 'r') as f:
        data = json.load(f)
    
    agents = data['agents']
    total_agents = len(agents)
    
    print(f"Generating RSA key pairs for {total_agents} agents...")
    
    # Generate keys for each agent
    for i, agent in enumerate(agents, 1):
        agent_id = agent['id']
        print(f"  [{i:2d}/{total_agents}] Generating keys for {agent_id}...")
        
        # Generate RSA key pair
        private_key, public_key = generate_rsa_key_pair()
        
        # Add keys to agent data
        agent['rsa_private_key'] = private_key
        agent['rsa_public_key'] = public_key
        
        print(f"      ✅ Keys generated for {agent_id}")
    
    # Save updated JSON file
    print(f"\nSaving updated agents file...")
    with open(agents_file, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"✅ Successfully added RSA key pairs to {total_agents} agents")
    print(f"📁 Updated file: {agents_file}")
    
    # Display sample of first agent's keys (for verification)
    first_agent = agents[0]
    print(f"\n📋 Sample key data for '{first_agent['id']}':")
    print(f"   Private key length: {len(first_agent['rsa_private_key'])} characters")
    print(f"   Public key length:  {len(first_agent['rsa_public_key'])} characters")
    print(f"   Private key starts: {first_agent['rsa_private_key'][:50]}...")
    print(f"   Public key starts:  {first_agent['rsa_public_key'][:50]}...")

if __name__ == "__main__":
    main()