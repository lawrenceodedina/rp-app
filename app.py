# /opt/rp-app/app.py
import os
import json
import base64
import uuid
import socket
from datetime import datetime
from flask import Flask, request, jsonify
import boto3
import psycopg2
 
app = Flask(__name__)
 
# AWS clients automatically use the EC2 instance's IAM role
s3 = boto3.client('s3')
secrets = boto3.client('secretsmanager')
ses = boto3.client('ses')
 
# Configuration from environment variables
RESUME_BUCKET = os.environ.get('RESUME_BUCKET')
DB_SECRET_NAME = os.environ.get('DB_SECRET_NAME')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL')
 
# Cache DB credentials in memory
_db_creds = None
 
def get_db_credentials():
    """Fetch DB credentials from Secrets Manager (cached after first call)"""
    global _db_creds
    if _db_creds is None:
        response = secrets.get_secret_value(SecretId=DB_SECRET_NAME)
        _db_creds = json.loads(response['SecretString'])
    return _db_creds
 
def get_db_connection():
    """Get a fresh database connection"""
    creds = get_db_credentials()
    return psycopg2.connect(
        host=creds['host'],
        port=creds['port'],
        database='portal',
        user=creds['username'],
        password=creds['password'],
        connect_timeout=5
    )
 
# CRITICAL: Health check endpoint - the ALB calls this constantly
@app.route('/health')
def health():
    """ALB hits this every 30s. Returns 200 if app is healthy."""
    try:
        conn = get_db_connection()
        conn.close()
        return jsonify({'status': 'healthy'}), 200
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 503
 
# Useful for testing load balancing later
@app.route('/whoami')
def whoami():
    """Returns the hostname so we can see WHICH instance handled the request"""
    return jsonify({
        'hostname': socket.gethostname(),
        'timestamp': datetime.utcnow().isoformat()
    })
 
# Status page (handy when debugging)
@app.route('/')
def index():
    return jsonify({
        'service': 'resume-portal',
        'status': 'ok',
        'instance': socket.gethostname()
    })
 
# Main submission endpoint
@app.route('/submit', methods=['POST', 'OPTIONS'])
def submit_application():
    if request.method == 'OPTIONS':
        return _cors_response('', 200)
    
    try:
        data = request.json
        full_name = data['fullName']
        email = data['email']
        phone = data['phone']
        position = data['position']
        skills = data['skills']
        resume_base64 = data['resume']
        original_filename = data['fileName']
        
        # Generate unique S3 key
        now = datetime.utcnow()
        unique_id = str(uuid.uuid4())[:8]
        s3_key = f"resumes/{now.year}/{now.month:02d}/{unique_id}_{original_filename}"
        
        # Upload PDF to S3
        resume_bytes = base64.b64decode(resume_base64)
        s3.put_object(
            Bucket=RESUME_BUCKET,
            Key=s3_key,
            Body=resume_bytes,
            ContentType='application/pdf'
        )
        
        # Insert into database
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO applications 
                    (full_name, email, phone, position, skills, resume_s3_key)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id;
            """, (full_name, email, phone, position, skills, s3_key))
            application_id = cursor.fetchone()[0]
            conn.commit()
            cursor.close()
        finally:
            conn.close()
        
        # Send confirmation email
        ses.send_email(
            Source=SENDER_EMAIL,
            Destination={'ToAddresses': [email]},
            Message={
                'Subject': {'Data': f'Application Received - {position}'},
                'Body': {'Text': {'Data': f"Hi {full_name}, thanks for applying! Application ID: {application_id}"}}
            }
        )
        
        return _cors_response({
            'message': 'Application submitted',
            'applicationId': application_id,
            'instance': socket.gethostname()
        }, 200)
        
    except Exception as e:
        app.logger.error(f"Submission failed: {e}")
        return _cors_response({'error': str(e)}, 500)
 
def _cors_response(body, status):
    """CORS-friendly response helper"""
    response = jsonify(body) if body else jsonify({})
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'OPTIONS,POST'
    response.status_code = status
    return response
 
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)

