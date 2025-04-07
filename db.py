import sqlite3

DB_PATH = 'bgpsms.db'

def dict_factory(cursor, row):
    """Convert sqlite3.Row to dict for easier access in templates"""
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = dict_factory  # Use dict_factory instead of sqlite3.Row
    return conn

def fetch_table_rows(table, limit=100):  # Increased default limit for better visibility
    conn = get_db_connection()
    rows = conn.execute(f'SELECT * FROM {table} ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    conn.close()
    return rows

# Function to add contacts from CSV to the contacts_queue table
def add_contacts_to_tables(csv_data):
    conn = get_db_connection()
    cursor = conn.cursor()

    for row in csv_data:
        name = row['name']
        phone = row['phone']
        state = row['state']

        # Insert into contacts_queue
        cursor.execute('INSERT INTO contacts_queue (name, phone, state) VALUES (?, ?, ?)', (name, phone, state))

    conn.commit()
    conn.close()

# Function to clear the content of a given table
def clear_table(table_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f'DELETE FROM {table_name}')
    conn.commit()
    conn.close()

# Function to add a new template to the templates table
def add_template_to_table(template_text):
    """
    Adds a new template to the templates table
    
    Args:
        template_text (str): The text content of the template
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO templates (content) VALUES (?)",
            (template_text,)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error adding template: {e}")
        raise e

# Function to delete a template from the templates table by ID
def delete_template_from_table(template_id):
    """
    Deletes a template from the templates table by ID
    
    Args:
        template_id (int): The ID of the template to delete
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM templates WHERE id = ?",
            (template_id,)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error deleting template: {e}")
        raise e
        
# Function to reset the auto-increment counter for a table
def reset_autoincrement(table_name):
    """
    Resets the auto-increment counter for a specific table
    
    Args:
        table_name (str): The name of the table to reset
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Check if the table is empty first
        count = cursor.execute(f"SELECT COUNT(*) as count FROM {table_name}").fetchone()
        
        # Only reset if the table is empty
        if count and count['count'] == 0:
            cursor.execute(f"DELETE FROM sqlite_sequence WHERE name = ?", (table_name,))
            conn.commit()
            
        conn.close()
    except Exception as e:
        print(f"Error resetting auto-increment for {table_name}: {e}")
        raise e
        
# Function to clear templates and reset the counter
def clear_templates():
    """
    Clear all templates and reset the auto-increment counter
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Delete all templates
        cursor.execute("DELETE FROM templates")
        
        # Reset the auto-increment counter
        cursor.execute("DELETE FROM sqlite_sequence WHERE name = 'templates'")
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error clearing templates: {e}")
        raise e
