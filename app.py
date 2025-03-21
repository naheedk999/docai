import streamlit as st
import boto3
import requests
import base64
import time
import os
import re
import ast
import json
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from io import BytesIO

# === Config (replace with your actual values) ===
REGION = st.secrets["REGION"]
USER_POOL_ID = st.secrets["USER_POOL_ID"]
CLIENT_ID = st.secrets["CLIENT_ID"]
API_URL = st.secrets["API_URL"]

st.set_page_config(layout="wide")

st.markdown("""
    <style>
        .main .block-container {
            max-width: 1100px;  /* Adjust this to your preferred content width */
            margin: auto;
            padding: 1rem 2rem;  /* Add some padding for breathing space */
            background-color: #1a1b1e; /* Keeps dark theme */
            border-radius: 12px; /* Optional rounded corners */
            box-shadow: 0 4px 12px rgba(0,0,0,0.2); /* Optional subtle shadow */
        }
        
        /* Optional - Style headings, buttons, and inputs to match */
        h1, h2, h3 {
            color: #4a90e2; /* Blue-ish for titles */
        }
        
        .stTextInput>div>div>input,
        .stTextArea>div>div>textarea,
        .stSelectbox>div>div>select {
            background-color: #2d2d2d;
            color: #fff;
            border: 1px solid #555;
        }
        
        .stButton>button {
            background-color: #4a6f8c;
            color: white;
            border: none;
            border-radius: 8px;
            padding: 8px 16px;
            transition: background-color 0.3s ease;
        }
        
        .stButton>button:hover {
            background-color: #5a7f9c;
        }
    </style>
""", unsafe_allow_html=True)

# === Login Function ===
def login_to_cognito(email, password):
    client = boto3.client('cognito-idp', region_name=REGION)
    try:
        auth_response = client.initiate_auth(
            ClientId=CLIENT_ID,
            AuthFlow='USER_PASSWORD_AUTH',
            AuthParameters={
                'USERNAME': email,
                'PASSWORD': password
            }
        )
        return auth_response['AuthenticationResult']['IdToken']
    except client.exceptions.NotAuthorizedException:
        st.error("❌ Incorrect username or password.")
    except Exception as e:
        st.error(f"❌ Unexpected error: {e}")
    return None

# === Pre-Briefing API Call (replace this URL later with the actual one you give me) ===
def generate_pre_briefing(patient_id, token):
    """url = f"{API_URL}/pre-report"  # You can update this later
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.post(url, json={"patient_id": patient_id}, headers=headers)
    
    if response.status_code == 200:
        return response.json()
    elif response.status_code == 404:
        return {"error": "Patient not found in database"}
    else:
        return {"error": f"Unexpected error: {response.status_code} - {response.text}"}"""
    return "{'response':'Coming Soon'}"

# === Helper Function: Poll for Transcription Result ===
def send_audio_to_transcription_api(file_bytes, filename, language, token, content_type):
    """Upload audio to S3 and start transcription."""
    # Step 1: Get pre-signed URL
    url = f"{API_URL}/generate-presigned-url"
    headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
    }
    presigned_response = requests.post(url, json={"filename": filename, "contentType": content_type}, headers=headers)
    if presigned_response.status_code != 200:
        st.error(f"❌ Failed to get upload URL: {presigned_response.status_code} - {presigned_response.text}")
        return None

    upload_data = presigned_response.json()
    upload_url = upload_data["upload_url"]
    s3_key = upload_data["s3_key"]

    # Step 2: Upload to S3
    upload_response = requests.put(upload_url, data=file_bytes, headers={"Content-Type": content_type})

    if upload_response.status_code not in [200, 204]:
        st.error(f"❌ Failed to upload file: {upload_response.status_code} - {upload_response.text}")
        return None

    # Step 3: Start transcription
    transcription_url = f"{API_URL}/start-transcription-s3"
    transcription_payload = {
        "s3_key": s3_key,
        "language": language
    }

    transcription_response = requests.post(
        transcription_url,
        headers=headers,
        json=transcription_payload
    )

    if transcription_response.status_code != 200:
        st.error(f"❌ Failed to start transcription: {transcription_response.status_code} - {transcription_response.text}")
        return None

    return transcription_response.json().get("job_name")

# === Helper Function: Poll for Transcription Result ===
def poll_transcription_status(job_name, token, max_retries=150, delay=5):
    """Polls transcription status and returns the text once completed."""
    url = f"{API_URL}/get-transcription?job_name={job_name}"
    headers = {"Authorization": f"Bearer {token}"}

    for attempt in range(max_retries):
        response = requests.get(url, headers=headers)

        if response.status_code == 401:
            st.error("❌ Unauthorized - check token.")
            return None
        elif response.status_code == 404:
            st.error(f"❌ Job not found.")
            return None
        elif response.status_code == 202:
            st.write(f"⏳ Transcription still in progress...")
            time.sleep(delay)
            continue
        elif response.status_code != 200:
            st.error(f"❌ Unexpected error fetching transcription status: {response.status_code} - {response.text}")
            return None

        # Success case - 200
        data = response.json()
        status = data.get("status")

        if status == "COMPLETED":
            return data.get("transcript", "")
        elif status == "FAILED":
            st.error(f"❌ Transcription job failed: {data.get('error', 'Unknown error')}")
            return None

        st.write(f"⏳ Waiting for transcription to complete... Retrying in {delay} seconds")
        time.sleep(delay)

    st.error("❌ Transcription timed out.")
    return None


def clean_llm_response(llm_response):
    """Extracts and parses the actual response from the LLM, converting it into a clean Python dictionary."""
    try:
        # Extract the inner response string
        response_str = llm_response.get('response', '')

        # Convert the string into a proper dictionary
        cleaned_dict = json.loads(response_str)

        return cleaned_dict

    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse LLM response: {e}")

# === Helper Function: Generate Patient Report ===
def generate_patient_report(transcript, token, language="en"):
    """Send transcript to get patient report summary."""
    url = f"{API_URL}/summary-patient"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "text": transcript,
        "language": language
    }
    response = requests.post(url, json=payload, headers=headers)
    
    if response.status_code != 200:
        st.error(f"❌ Failed to generate report: {response.status_code} - {response.text}")
        return None

    try:
        # Extract dictionary from the response text
        response_dictt = json.loads(response.text)
        report_dict = clean_llm_response(response_dictt)
        return report_dict
    except ValueError as e:
        st.error(f"❌ Failed to process report response: {e}")
        return None
    
def generate_doctor_report(transcript, token, language="en"):
    """Send transcript to get doctor report summary."""
    url = f"{API_URL}/summary-doctor"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "text": transcript,
        "language": language
    }
    response = requests.post(url, json=payload, headers=headers)
    
    if response.status_code != 200:
        st.error(f"❌ Failed to generate report: {response.status_code} - {response.text}")
        return None

    try:
        # Extract dictionary from the response text
        response_dictt = json.loads(response.text)
        report_dict = clean_llm_response(response_dictt)
        return report_dict
    except ValueError as e:
        st.error(f"❌ Failed to process report response: {e}")
        return None
    
    
def generate_pdf(info, report, language):
    """Generate a styled patient report PDF in English or Italian using ReportLab."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)

    # Styles
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleStyle", parent=styles["Heading1"], textColor=colors.darkgreen, alignment=1, spaceAfter=12)
    section_title_style = ParagraphStyle("SectionTitle", parent=styles["Heading2"], textColor=colors.darkblue, spaceAfter=8)
    body_style = styles["BodyText"]

    elements = []

    # Translation for Sections
    translations = {
        "en": {
            "report_title": "Medical Report",
            "patient_name": "Patient Name:",
            "id_number": "ID Number:",
            "dob": "Date of Birth:",
            "reason_for_visit": "Reason for Visit:",
            "chief_complaint": "Chief Complaint & History of Present Illness",
            "clinical_findings": "Clinical Examination & Diagnostic Findings",
            "diagnosis_treatment": "Diagnosis and Treatment Plan",
            "medications": "Medication Prescription",
            "follow_up": "Follow-Up & Recommendations",
            "visit_date": "Visit Date:"
        },
        "it": {
            "report_title": "Referto Medico",
            "patient_name": "Nome Paziente:",
            "id_number": "Numero ID:",
            "dob": "Data di Nascita:",
            "reason_for_visit": "Motivo della Visita:",
            "chief_complaint": "Anamnesi e Sintomatologia",
            "clinical_findings": "Esame Clinico e Risultati Diagnostici",
            "diagnosis_treatment": "Diagnosi e Piano di Trattamento",
            "medications": "Prescrizione Medica",
            "follow_up": "Follow-Up e Raccomandazioni",
            "visit_date": "Data della Visita:"
        }
    }

    labels = translations.get(language, translations["en"])  # Default to English if language is missing

    # Header (Doctor Info + Logo)
    header_table = []

    # Logo (if exists)
    logo_path = info.get("logo_path")
    if logo_path:
        logo = Image(logo_path, width=230, height=80)
    else:
        logo = Paragraph("", body_style)

    doctor_info = [
        Paragraph(f"<strong>{info['doctor_name']}</strong>", body_style),
        Paragraph(f"Specialist in {info['specialization']}", body_style),
        Paragraph(f"Contact: {info['contact']}", body_style),
        Paragraph(f"Email: {info['email']}", body_style)
    ]

    header_table.append([logo, doctor_info])
    table = Table(header_table, colWidths=[400, 150])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE")
    ]))
    elements.append(table)

    elements.append(Spacer(1, 20))

    # Title
    elements.append(Paragraph(labels["report_title"], title_style))

    # Patient Information Table
    patient_table = [
        [labels["patient_name"], info["patient"]["name"]],
        [labels["id_number"], info["patient"]["med_number"]],
        [labels["dob"], info["patient"]["birth_date"]],
        [labels["reason_for_visit"], report["reason_for_visit"]]
    ]
    table = Table(patient_table, colWidths=[150, 350])
    table.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 1, colors.grey),
        ('BACKGROUND', (0, 0), (-1, -1), colors.whitesmoke),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
        ('PADDING', (0, 0), (-1, -1), 5)
    ]))
    elements.append(table)

    elements.append(Spacer(1, 20))

    # Sections (Chief Complaint, Findings, etc.)
    def add_section(title, content):
        elements.append(Paragraph(title, section_title_style))
        elements.append(Paragraph(content or "N/A", body_style))
        elements.append(Spacer(1, 12))

    add_section(labels["chief_complaint"], report["chief_complaint_history"])
    add_section(labels["clinical_findings"], report["clinical_findings"])
    add_section(labels["diagnosis_treatment"], report["diagnosis_treatment_plan"])
    add_section(labels["medications"], report["medication_prescription"])
    add_section(labels["follow_up"], report["follow_up_recommendations"])

    # Footer
    elements.append(Spacer(1, 50))

    footer_table = [
        [f"{labels['visit_date']} {info['visit_date']}", f"{info['doctor_name']} - {info['specialization']}"]
    ]
    table = Table(footer_table, colWidths=[250, 250])
    table.setStyle(TableStyle([
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT')
    ]))
    elements.append(table)

    # Generate PDF
    doc.build(elements)
    buffer.seek(0)
    return buffer
    
# === Patient Visit Tab (Enhanced) ===
def patient_visit_tab():
    # Add custom CSS for better styling
    st.markdown("""
        <style>
        .stButton>button {
            width: 100%;
            border-radius: 5px;
            height: 3em;
            margin-top: 1em;
        }
        .stTextArea>div>div>textarea {
            border-radius: 5px;
        }
        .stSelectbox>div>div>select {
            border-radius: 5px;
        }
        .main .block-container {
            padding-top: 2rem;
        }
        .stMarkdown h3 {
            color: #1f77b4;
            padding-top: 1rem;
        }
        .stMarkdown h2 {
            color: #2c3e50;
        }
        </style>
    """, unsafe_allow_html=True)

    # Create two columns for audio input options
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("#### 🎤 Record Audio")
        record_disabled = st.session_state.audio_source == "upload"
        recorded_audio = st.audio_input("Record your visit notes", disabled=record_disabled)

    with col2:
        st.markdown("#### 📁 Upload Audio")
        upload_disabled = st.session_state.audio_source == "record"
        uploaded_file = st.file_uploader("Upload audio file (MP3/WAV/M4A)", type=["mp3", "wav", "m4a"], disabled=upload_disabled)

    if recorded_audio and st.session_state.audio_source != "record":
        st.session_state.audio_source = "record"
        st.rerun()

    if uploaded_file and st.session_state.audio_source != "upload":
        st.session_state.audio_source = "upload"
        st.rerun()

    # Language selection with better styling
    st.markdown("#### 🌐 Select Language")
    language = st.selectbox("Choose the language for transcription", ["en", "it"])

    # Transcription button with better styling
    if st.button("🎯 Generate Transcript", type="primary"):
        if not recorded_audio and not uploaded_file:
            st.error("❌ Please record or upload an audio file.")
        else:
            with st.spinner("⏳ Uploading and starting transcription..."):
                if recorded_audio:
                    filename = "recorded-visit.wav"
                    file_bytes = recorded_audio.getvalue()
                    content_type = "audio/wav"
                else:
                    filename = uploaded_file.name
                    file_bytes = uploaded_file.getvalue()
                    content_type = f"audio/{uploaded_file.type}"

                job_name = send_audio_to_transcription_api(file_bytes, filename, language, st.session_state.jwt_token, content_type)

                if job_name:
                    st.success(f"✅ Transcription started!")
                    transcript = poll_transcription_status(job_name, st.session_state.jwt_token)
                    if transcript:
                        st.success("✅ Transcription Completed!")
                        st.session_state.current_transcript = transcript
                        st.rerun()
                    else:
                        st.error("❌ Failed to retrieve transcription result.")

    # Display transcript and generate reports button only if transcript is available
    if 'current_transcript' in st.session_state and st.session_state.current_transcript:
        st.markdown("### 📝 Transcript")
        st.markdown("Review and edit the transcript if needed:")
        edited_transcript = st.text_area(
            "Transcript content",
            value=st.session_state.current_transcript,
            height=400,
            key="transcript_editor"
        )
        
        if edited_transcript != st.session_state.current_transcript:
            st.session_state.current_transcript = edited_transcript
        
        if st.button("📊 Generate Reports", type="primary"):
            with st.spinner("⏳ Generating reports..."):
                doctor_report = generate_doctor_report(st.session_state.current_transcript, st.session_state.jwt_token, language)
                patient_report = generate_patient_report(st.session_state.current_transcript, st.session_state.jwt_token, language)
                if patient_report and doctor_report:
                    st.session_state.patient_report = patient_report
                    st.session_state.doctor_report = doctor_report
                    st.success("✅ Reports Generated Successfully!")
                    st.rerun()

    # Display report data in editable fields if available
    if st.session_state.patient_report and st.session_state.doctor_report:
        st.markdown("### 📋 Report Summary")
        st.markdown("Review and edit the report details below:")
        st.markdown("---")

        report_tab1, report_tab2 = st.tabs(["🏥 Patient Report", "👨‍⚕️ Doctor Report"])
        
        with report_tab1:
            # Patient Report
            patient_report = st.session_state.patient_report
            
            reason_for_visit = st.text_area(
                "Reason for Visit",
                value=patient_report.get("reason_for_visit", ""),
                height=100,
                key="patient_reason_visit"
            )
            
            chief_complaint = st.text_area(
                "Chief Complaint History",
                value=patient_report.get("chief_complaint_history", ""),
                height=100,
                key="patient_chief_complaint"
            )
            
            clinical_findings = st.text_area(
                "Clinical Findings",
                value=patient_report.get("clinical_findings", ""),
                height=100,
                key="patient_clinical_findings"
            )
            
            diagnosis_plan = st.text_area(
                "Diagnosis & Treatment Plan",
                value=patient_report.get("diagnosis_treatment_plan", ""),
                height=100,
                key="patient_diagnosis_plan"
            )
            
            medication = st.text_area(
                "Medication & Prescription",
                value=patient_report.get("medication_prescription", ""),
                height=100,
                key="patient_medication"
            )
            
            follow_up = st.text_area(
                "Follow-up Recommendations",
                value=patient_report.get("follow_up_recommendations", ""),
                height=100,
                key="patient_follow_up"
            )

            if st.button("Save Patient Report", type="primary", key="save_patient"):
                updated_report = {
                    "reason_for_visit": reason_for_visit,
                    "chief_complaint_history": chief_complaint,
                    "clinical_findings": clinical_findings,
                    "diagnosis_treatment_plan": diagnosis_plan,
                    "medication_prescription": medication,
                    "follow_up_recommendations": follow_up
                }
                st.session_state.patient_report = updated_report
                st.success("Patient report saved successfully")
            
            # Add PDF generation button for patient report
            if st.button("Generate PDF", type="secondary", key="gen_patient_pdf"):
                info = {
                    "doctor_name": st.session_state.doctor_settings["doctor_name"],
                    "specialization": st.session_state.doctor_settings["specialization"],
                    "contact": st.session_state.doctor_settings["contact"],
                    "email": st.session_state.doctor_settings["email"],
                    "visit_date": time.strftime("%Y-%m-%d"),
                    "type_report": "Patient",
                    "logo_path": "logo.png",
                    "patient": {
                        "name": patient_name,
                        "birth_date": date_of_birth.strftime("%Y-%m-%d"),
                        "med_number": patient_id
                    }
                }
                pdf_buffer = generate_pdf(info, st.session_state.patient_report,language)
                st.download_button(
                    label="Download PDF",
                    data=pdf_buffer,
                    file_name=f"patient_report_{patient_id}.pdf",
                    mime="application/pdf"
                )

        with report_tab2:
            # Doctor Report
            doctor_report = st.session_state.doctor_report
            
            reason_for_visit = st.text_area(
                "Reason for Visit",
                value=doctor_report.get("reason_for_visit", ""),
                height=100,
                key="doctor_reason_visit"
            )
            
            chief_complaint = st.text_area(
                "Chief Complaint History",
                value=doctor_report.get("chief_complaint_history", ""),
                height=100,
                key="doctor_chief_complaint"
            )
            
            clinical_findings = st.text_area(
                "Clinical Findings",
                value=doctor_report.get("clinical_findings", ""),
                height=100,
                key="doctor_clinical_findings"
            )
            
            diagnosis_plan = st.text_area(
                "Diagnosis & Treatment Plan",
                value=doctor_report.get("diagnosis_treatment_plan", ""),
                height=100,
                key="doctor_diagnosis_plan"
            )
            
            medication = st.text_area(
                "Medication & Prescription",
                value=doctor_report.get("medication_prescription", ""),
                height=100,
                key="doctor_medication"
            )
            
            follow_up = st.text_area(
                "Follow-up Recommendations",
                value=doctor_report.get("follow_up_recommendations", ""),
                height=100,
                key="doctor_follow_up"
            )

            if st.button("Save Doctor Report", type="primary", key="save_doctor"):
                updated_report = {
                    "reason_for_visit": reason_for_visit,
                    "chief_complaint_history": chief_complaint,
                    "clinical_findings": clinical_findings,
                    "diagnosis_treatment_plan": diagnosis_plan,
                    "medication_prescription": medication,
                    "follow_up_recommendations": follow_up
                }
                st.session_state.doctor_report = updated_report
                st.success("Doctor report saved successfully")
            
            # Add PDF generation button for doctor report
            if st.button("Generate PDF", type="secondary", key="gen_doctor_pdf"):
                info = {
                    "doctor_name": st.session_state.doctor_settings["doctor_name"],
                    "specialization": st.session_state.doctor_settings["specialization"],
                    "contact": st.session_state.doctor_settings["contact"],
                    "email": st.session_state.doctor_settings["email"],
                    "visit_date": time.strftime("%Y-%m-%d"),
                    "type_report": "Doctor",
                    "logo_path": "logo.png",
                    "patient": {
                        "name": patient_name,
                        "birth_date": date_of_birth.strftime("%Y-%m-%d"),
                        "med_number": patient_id
                    }
                }
                pdf_buffer = generate_pdf(info, st.session_state.doctor_report,language)
                st.download_button(
                    label="Download PDF",
                    data=pdf_buffer,
                    file_name=f"doctor_report_{patient_id}.pdf",
                    mime="application/pdf"
                )

# === Session Setup ===
if 'jwt_token' not in st.session_state:
    st.session_state.jwt_token = None
if 'pre_briefing_data' not in st.session_state:
    st.session_state.pre_briefing_data = None
if 'audio_source' not in st.session_state:
    st.session_state.audio_source = None
if 'patient_report' not in st.session_state:
    st.session_state.patient_report = None
if 'doctor_report' not in st.session_state:
    st.session_state.doctor_report = None
if 'current_transcript' not in st.session_state:
    st.session_state.current_transcript = None
if 'doctor_settings' not in st.session_state:
    st.session_state.doctor_settings = {
        "doctor_name": "Dr. Naheed Khan",
        "specialization": "Cardiology",
        "contact": "123-456-7890",
        "email": "doctor@example.com"
    }
if 'current_page' not in st.session_state:
    st.session_state.current_page = "home"

# === Login Screen (if not logged in) ===
if st.session_state.jwt_token is None:
    st.markdown("""
        <style>
        /* Reset default theme */
        .main .block-container {
            padding: 1rem 2rem;
            max-width: none;
        }
        
        /* Dark theme colors */
        .stApp {
            background-color: #1a1b1e;
            color: #ffffff;
        }
        
        /* Title styling */
        .app-title {
            color: #ffffff;
            font-size: 2.5rem;
            font-weight: 600;
            text-align: center;
            margin: 2rem 0;
        }
        
        /* Input fields styling */
        .stTextInput input {
            background-color: #2d2d2d;
            border: 1px solid #404040;
            color: white;
            border-radius: 8px;
            padding: 8px 12px;
        }
        
        /* Button styling */
        .stButton button {
            background-color: #4a6f8c !important;
            color: white !important;
            border: none !important;
            border-radius: 8px !important;
            padding: 8px 16px !important;
        }
        
        .stButton button:hover {
            background-color: #5a7f9c !important;
        }
        
        /* Hide default header */
        #MainMenu {visibility: hidden;}
        header {visibility: hidden;}
        </style>
    """, unsafe_allow_html=True)

    # Center the content
    col1, col2, col3 = st.columns([1, 2, 1])

    with col2:
        st.markdown('<h1 class="app-title">Salus.tech</h1>', unsafe_allow_html=True)
        with st.form("login_form", clear_on_submit=False):
            email = st.text_input("📧 Email", placeholder="Enter your email")
            password = st.text_input("🔒 Password", type="password", placeholder="Enter your password")
            submit = st.form_submit_button("🔐 Login", use_container_width=True)
            if submit:
                with st.spinner("🔄 Logging in..."):
                    token = login_to_cognito(email, password)
                    if token:
                        st.session_state.jwt_token = token
                        st.success("✅ Logged in successfully!")
                        st.rerun()

# === Home Page (after login) ===
else:
    st.markdown("""
        <style>
        /* Reset default theme */
        .main .block-container {
            padding: 1rem 2rem;
            max-width: none;
        }
        
        /* Dark theme colors */
        .stApp {
            background-color: #1a1b1e;
            color: #ffffff;
        }
        
        /* Navigation styling */
        .nav-button {
            display: inline-flex;
            align-items: center;
            background-color: rgba(255, 255, 255, 0.1);
            color: white;
            padding: 8px 16px;
            border-radius: 8px;
            text-decoration: none;
            transition: background-color 0.3s;
            border: none;
            cursor: pointer;
        }
        
        .nav-button:hover {
            background-color: rgba(255, 255, 255, 0.2);
        }
        
        /* Title styling */
        .app-title {
            color: #4a6f8c;
            font-size: 2.5rem;
            font-weight: 600;
            text-align: center;
            margin: 0;
            padding: 0;
        }
        
        /* Input fields styling */
        .stTextInput input {
            background-color: #2d2d2d;
            border: 1px solid #404040;
            color: white;
            border-radius: 8px;
        }
        
        .stDateInput input {
            background-color: #2d2d2d;
            border: 1px solid #404040;
            color: white;
            border-radius: 8px;
        }

        /* Error message styling */
        .stAlert {
            background-color: rgba(255, 87, 87, 0.2);
            color: #ff8080;
        }

        /* Success message styling */
        div[data-baseweb="notification"] {
            background-color: rgba(45, 200, 117, 0.2);
            color: #2dc875;
        }
        
        /* Button styling */
        .stButton button {
            background-color: #4a6f8c !important;
            color: white !important;
            border: none !important;
            border-radius: 8px !important;
            padding: 8px 16px !important;
            width: auto !important;
            min-width: 300px !important;
            margin: 0 auto !important;
            display: block !important;
        }
                
        button[data-testid="stButtonSettings_nav"] {
            background-color: transparent !important;
            color: white !important;
            font-size: 24px !important;
            width: 48px !important;
            height: 48px !important;
            min-width: unset !important;
            border: none !important;
            padding: 8px !important;
            display: inline-flex !important;
            align-items: center;
            justify-content: center;
            border-radius: 50% !important;
            transition: background-color 0.3s;
        }



        .stButton button[data-testid="stButtonHome_nav"]:hover {
            background-color: rgba(255, 255, 255, 0.1) !important;
        }
            
        .stButton button:hover {
            background-color: #5a7f9c !important;
        }
        
        /* Hide default header */
        #MainMenu {visibility: hidden;}
        header {visibility: hidden;}

        /* Form styling */
        .stForm > div {
            max-width: none;
            padding: 1rem;
        }

        /* Text area styling */
        .stTextArea textarea {
            background-color: #2d2d2d;
            color: white;
            border: 1px solid #404040;
        }
        </style>
    """, unsafe_allow_html=True)

    # Navigation bar
    nav_col1, nav_col2, nav_col3 = st.columns([1, 2, 1])
    
    with nav_col2:
        st.markdown("<h1 class='app-title' style='text-align: center;'>Doctor AI Assistant</h1>", unsafe_allow_html=True)
    
    with nav_col3:
        if st.button("Doctor's credentials", key="stButtonSettings_nav", help="Settings"):
            st.session_state.current_page = "settings"
            st.rerun()

    st.markdown("<hr style='margin: 1rem 0; border-color: #404040;'>", unsafe_allow_html=True)

    if st.session_state.current_page == "home":
        # Patient Info Inputs in columns
        col1, col2, col3 = st.columns([1, 1, 1])
        
        with col1:
            patient_id = st.text_input("Patient ID", placeholder="Enter patient ID")
        with col2:
            patient_name = st.text_input("Patient Name", placeholder="Enter patient name")
        with col3:
            date_of_birth = st.date_input("Date of Birth")

        patient_visit_tab()

    elif st.session_state.current_page == "settings":
        # Navigation for settings page
        nav_col1, nav_col2, nav_col3 = st.columns([1, 10, 1])
        
        with nav_col2:
            st.markdown("<h1 class='app-title'>Settings</h1>", unsafe_allow_html=True)

        #st.markdown("<hr style='margin: 1rem 0; border-color: #404040;'>", unsafe_allow_html=True)
        
        # Doctor Information Form
        with st.form("doctor_settings_form"):
            st.markdown("#### 👨‍⚕️ Doctor Information")
            
            doctor_name = st.text_input(
                "Doctor Name",
                value=st.session_state.doctor_settings["doctor_name"],
                key="doctor_name_input"
            )
            
            specialization = st.text_input(
                "Specialization",
                value=st.session_state.doctor_settings["specialization"],
                key="specialization_input"
            )
            
            contact = st.text_input(
                "Contact Number",
                value=st.session_state.doctor_settings["contact"],
                key="contact_input"
            )
            
            email = st.text_input(
                "Email",
                value=st.session_state.doctor_settings["email"],
                key="email_input"
            )
            
            submitted = st.form_submit_button("💾 Save and Back")
            
            if submitted:
                st.session_state.doctor_settings = {
                    "doctor_name": doctor_name,
                    "specialization": specialization,
                    "contact": contact,
                    "email": email
                }
                st.success("✅ Settings saved successfully!")
                st.session_state.current_page = "home"
                st.rerun()
