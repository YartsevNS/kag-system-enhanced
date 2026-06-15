import asyncio
from neo4j import GraphDatabase
import os

NEO4J_URI = "bolt://192.168.50.18:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "password")  # default

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

def check():
    with driver.session() as session:
        res = session.run("MATCH (e:Entity) RETURN count(e) as count")
        print("Total entities:", res.single()["count"])
        
        res = session.run("MATCH (e:Entity) RETURN e.name as name LIMIT 10")
        print("Sample entities:", [r["name"] for r in res])

check()
driver.close()
