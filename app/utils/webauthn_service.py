"""
WebAuthn / FIDO2 Service for AttendX
Handles biometric credential registration, authentication, and 5-minute early verification.
"""
import os
import base64
import json
from datetime import datetime, timedelta
from flask import request
from app import db
from app.models import (
    User, Student, ClassSession, WebAuthnCredential, 
    EarlyBiometricVerification, WebAuthnChallenge
)
import webauthn
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorAttachment,
    UserVerificationRequirement,
    ResidentKeyRequirement,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialType
)
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes, options_to_json


def get_rp_id():
    """Get Relying Party ID based on current request host"""
    host = request.host.split(':')[0]
    return host if host else 'localhost'


def get_origin():
    """Get Expected Origin for WebAuthn ceremony"""
    # e.g. http://localhost:5000 or https://attendx.edu
    return request.host_url.rstrip('/')


def generate_registration_ceremony(user, nickname="My Device Fingerprint"):
    """
    Generate WebAuthn registration options for a user
    """
    rp_id = get_rp_id()
    rp_name = "AttendX Smart Attendance"
    
    # Exclude existing credentials
    existing_creds = WebAuthnCredential.query.filter_by(user_id=user.id, is_active=True).all()
    exclude_credentials = []
    for cred in existing_creds:
        try:
            cred_id_bytes = base64url_to_bytes(cred.credential_id)
            exclude_credentials.append(PublicKeyCredentialDescriptor(
                type=PublicKeyCredentialType.PUBLIC_KEY,
                id=cred_id_bytes
            ))
        except Exception:
            pass
            
    # Prefer platform authenticator (fingerprint reader, Touch ID, Face ID, Windows Hello)
    authenticator_selection = AuthenticatorSelectionCriteria(
        authenticator_attachment=AuthenticatorAttachment.PLATFORM,
        user_verification=UserVerificationRequirement.PREFERRED,
        resident_key=ResidentKeyRequirement.PREFERRED
    )
    
    # Generate options
    user_id_bytes = user.id.encode('utf-8')
    options = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name=rp_name,
        user_id=user_id_bytes,
        user_name=user.email,
        user_display_name=user.full_name,
        authenticator_selection=authenticator_selection,
        exclude_credentials=exclude_credentials if exclude_credentials else None,
        timeout=60000
    )
    
    # Store challenge in database
    challenge_b64 = bytes_to_base64url(options.challenge)
    expires_at = datetime.utcnow() + timedelta(minutes=5)
    
    # Remove stale challenges for this user
    WebAuthnChallenge.query.filter_by(user_id=user.id, challenge_type='registration').delete()
    
    challenge_record = WebAuthnChallenge(
        user_id=user.id,
        challenge=challenge_b64,
        challenge_type='registration',
        expires_at=expires_at
    )
    db.session.add(challenge_record)
    db.session.commit()
    
    return json.loads(options_to_json(options))


def verify_registration_ceremony(user, credential_data, nickname="My Device Fingerprint"):
    """
    Verify WebAuthn registration response and save the public key credential
    """
    # Fetch active challenge
    challenge_record = WebAuthnChallenge.query.filter_by(
        user_id=user.id,
        challenge_type='registration'
    ).order_by(WebAuthnChallenge.created_at.desc()).first()
    
    if not challenge_record or challenge_record.expires_at < datetime.utcnow():
        return {'success': False, 'error': 'Registration challenge expired. Please try again.'}
        
    expected_challenge = base64url_to_bytes(challenge_record.challenge)
    rp_id = get_rp_id()
    origin = get_origin()
    
    try:
        credential_json = json.dumps(credential_data) if isinstance(credential_data, dict) else credential_data
        verified_registration = webauthn.verify_registration_response(
            credential=credential_json,
            expected_challenge=expected_challenge,
            expected_origin=origin,
            expected_rp_id=rp_id,
            require_user_verification=False  # Allow fallback or preferred verification
        )
        
        # Save credential
        cred_id_str = bytes_to_base64url(verified_registration.credential_id)
        pub_key_str = bytes_to_base64url(verified_registration.credential_public_key)
        aaguid_str = str(verified_registration.aaguid) if verified_registration.aaguid else None
        
        # Remove challenge
        db.session.delete(challenge_record)
        
        new_credential = WebAuthnCredential(
            user_id=user.id,
            credential_id=cred_id_str,
            public_key=pub_key_str,
            sign_count=verified_registration.sign_count,
            nickname=nickname,
            aaguid=aaguid_str,
            last_used_at=datetime.utcnow()
        )
        db.session.add(new_credential)
        db.session.commit()
        
        return {
            'success': True,
            'message': 'Biometric authenticator successfully registered!',
            'credential_id': cred_id_str,
            'nickname': nickname
        }
    except Exception as e:
        db.session.rollback()
        return {'success': False, 'error': f'Verification failed: {str(e)}'}


def generate_early_verification_ceremony(student_user, session_id):
    """
    Generate WebAuthn authentication options for 5-minute early verification
    """
    session = ClassSession.query.get(session_id)
    if not session:
        return {'success': False, 'error': 'Class session not found'}
        
    # Check if student is enrolled
    from app.models import Enrollment
    enrollment = Enrollment.query.filter_by(
        student_id=student_user.id,
        class_instance_id=session.class_instance_id,
        status='active'
    ).first()
    if not enrollment:
        return {'success': False, 'error': 'You are not enrolled in this class'}
        
    # Check 5-minute early verification window
    now = datetime.now()
    if not session.is_early_verification_open(now):
        start = session.start_datetime
        window_start = session.early_window_start
        window_start_str = window_start.strftime('%I:%M %p') if window_start else '5 minutes before class'
        return {
            'success': False,
            'error': f'Early verification opens exactly 5 minutes before class ({window_start_str}).'
        }
        
    # Get user credentials
    credentials = WebAuthnCredential.query.filter_by(user_id=student_user.id, is_active=True).all()
    if not credentials:
        return {
            'success': False,
            'error': 'No biometric credentials registered. Please register your fingerprint in Profile & Security first.',
            'needs_registration': True
        }
        
    allow_credentials = []
    for cred in credentials:
        try:
            allow_credentials.append(PublicKeyCredentialDescriptor(
                type=PublicKeyCredentialType.PUBLIC_KEY,
                id=base64url_to_bytes(cred.credential_id)
            ))
        except Exception:
            pass
            
    rp_id = get_rp_id()
    options = webauthn.generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow_credentials,
        user_verification=UserVerificationRequirement.PREFERRED,
        timeout=60000
    )
    
    # Store challenge
    challenge_b64 = bytes_to_base64url(options.challenge)
    expires_at = datetime.utcnow() + timedelta(minutes=5)
    
    WebAuthnChallenge.query.filter_by(
        user_id=student_user.id,
        challenge_type='early_verification',
        session_id=session_id
    ).delete()
    
    challenge_record = WebAuthnChallenge(
        user_id=student_user.id,
        challenge=challenge_b64,
        session_id=session_id,
        challenge_type='early_verification',
        expires_at=expires_at
    )
    db.session.add(challenge_record)
    db.session.commit()
    
    return {
        'success': True,
        'options': json.loads(options_to_json(options)),
        'session': {
            'id': session.id,
            'class_name': session.class_instance.course.name,
            'class_code': session.class_instance.course.code,
            'start_time': session.start_time
        }
    }


def verify_early_verification_ceremony(student_user, session_id, credential_data):
    """
    Verify WebAuthn authentication for early verification and record EarlyBiometricVerification
    """
    session = ClassSession.query.get(session_id)
    if not session:
        return {'success': False, 'error': 'Session not found'}
        
    challenge_record = WebAuthnChallenge.query.filter_by(
        user_id=student_user.id,
        challenge_type='early_verification',
        session_id=session_id
    ).order_by(WebAuthnChallenge.created_at.desc()).first()
    
    if not challenge_record or challenge_record.expires_at < datetime.utcnow():
        return {'success': False, 'error': 'Verification challenge expired. Please retry.'}
        
    cred_id = credential_data.get('id') if isinstance(credential_data, dict) else None
    if not cred_id:
        return {'success': False, 'error': 'Invalid credential response'}
        
    # Find credential
    cred = WebAuthnCredential.query.filter_by(
        user_id=student_user.id,
        credential_id=cred_id,
        is_active=True
    ).first()
    
    if not cred:
        return {'success': False, 'error': 'Matching registered credential not found'}
        
    expected_challenge = base64url_to_bytes(challenge_record.challenge)
    rp_id = get_rp_id()
    origin = get_origin()
    
    try:
        credential_json = json.dumps(credential_data) if isinstance(credential_data, dict) else credential_data
        verified_auth = webauthn.verify_authentication_response(
            credential=credential_json,
            expected_challenge=expected_challenge,
            expected_origin=origin,
            expected_rp_id=rp_id,
            credential_public_key=base64url_to_bytes(cred.public_key),
            credential_current_sign_count=cred.sign_count,
            require_user_verification=False
        )
        
        # Update sign count
        cred.sign_count = verified_auth.new_sign_count
        cred.last_used_at = datetime.utcnow()
        
        # Calculate expiry (valid through scheduled class session end time + 30m grace)
        session_end = session.end_datetime or (datetime.now() + timedelta(hours=1))
        expires_at = session_end + timedelta(minutes=30)
        
        # Record early verification (or update existing)
        early_rec = EarlyBiometricVerification.query.filter_by(
            student_id=student_user.id,
            session_id=session.id
        ).first()
        
        if not early_rec:
            early_rec = EarlyBiometricVerification(
                student_id=student_user.id,
                session_id=session.id,
                credential_id=cred.credential_id,
                verified_at=datetime.utcnow(),
                expires_at=expires_at,
                status='valid'
            )
            db.session.add(early_rec)
        else:
            early_rec.verified_at = datetime.utcnow()
            early_rec.expires_at = expires_at
            early_rec.status = 'valid'
            early_rec.credential_id = cred.credential_id
            
        # Clean up challenge
        db.session.delete(challenge_record)
        db.session.commit()
        
        return {
            'success': True,
            'message': 'Identity verified! When class opens, simply scan the professor’s rotating QR to mark attendance.',
            'verified_at': early_rec.verified_at.isoformat(),
            'session_id': session.id
        }
    except Exception as e:
        db.session.rollback()
        return {'success': False, 'error': f'Biometric assertion failed: {str(e)}'}


def check_student_early_verification(student_id, session_id):
    """
    Check if a student completed a valid early verification for a session
    """
    verification = EarlyBiometricVerification.query.filter_by(
        student_id=student_id,
        session_id=session_id,
        status='valid'
    ).first()
    
    if not verification:
        return {'verified': False, 'reason': 'No early biometric record'}
        
    if verification.expires_at < datetime.utcnow():
        verification.status = 'expired'
        db.session.commit()
        return {'verified': False, 'reason': 'Early verification expired'}
        
    return {
        'verified': True,
        'verification_id': verification.id,
        'verified_at': verification.verified_at
    }


def consume_early_verification(student_id, session_id):
    """Mark the early verification as consumed when attendance is recorded"""
    verification = EarlyBiometricVerification.query.filter_by(
        student_id=student_id,
        session_id=session_id,
        status='valid'
    ).first()
    if verification:
        verification.status = 'consumed'
        db.session.commit()
        return True
    return False
