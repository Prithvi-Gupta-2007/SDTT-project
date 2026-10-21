"""
WebAuthn and Biometric Verification Routes for AttendX
"""
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime
from app import db
from app.models import WebAuthnCredential, ClassSession
from app.utils.webauthn_service import (
    generate_registration_ceremony,
    verify_registration_ceremony,
    generate_early_verification_ceremony,
    verify_early_verification_ceremony,
    check_student_early_verification
)

webauthn_bp = Blueprint('webauthn', __name__)


@webauthn_bp.route('/register/options', methods=['POST'])
@login_required
def register_options():
    """Generate WebAuthn registration options"""
    try:
        data = request.get_json() or {}
        nickname = data.get('nickname', 'Device Biometric Authenticator')
        options = generate_registration_ceremony(current_user, nickname)
        return jsonify({'success': True, 'options': options})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@webauthn_bp.route('/register/verify', methods=['POST'])
@login_required
def register_verify():
    """Verify WebAuthn registration response and save credential"""
    try:
        data = request.get_json()
        if not data or 'credential' not in data:
            return jsonify({'success': False, 'error': 'Credential data missing'}), 400
            
        credential_data = data.get('credential')
        nickname = data.get('nickname', 'Device Biometric Authenticator')
        
        result = verify_registration_ceremony(current_user, credential_data, nickname)
        return jsonify(result), (200 if result.get('success') else 400)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@webauthn_bp.route('/early-verify/options', methods=['POST'])
@login_required
def early_verify_options():
    """Generate WebAuthn challenge for 5-minute early verification"""
    if current_user.role != 'student':
        return jsonify({'success': False, 'error': 'Only students can perform early verification'}), 403
        
    data = request.get_json() or {}
    session_id = data.get('session_id')
    if not session_id:
        return jsonify({'success': False, 'error': 'session_id is required'}), 400
        
    result = generate_early_verification_ceremony(current_user, session_id)
    return jsonify(result), (200 if result.get('success') else 400)


@webauthn_bp.route('/early-verify/verify', methods=['POST'])
@login_required
def early_verify_verify():
    """Verify WebAuthn response and record 5-minute early verification"""
    if current_user.role != 'student':
        return jsonify({'success': False, 'error': 'Only students can perform early verification'}), 403
        
    data = request.get_json() or {}
    session_id = data.get('session_id')
    credential_data = data.get('credential')
    
    if not session_id or not credential_data:
        return jsonify({'success': False, 'error': 'session_id and credential required'}), 400
        
    result = verify_early_verification_ceremony(current_user, session_id, credential_data)
    return jsonify(result), (200 if result.get('success') else 400)


@webauthn_bp.route('/early-verify/status/<session_id>', methods=['GET'])
@login_required
def early_verify_status(session_id):
    """Check if current student has completed early verification for a session"""
    if current_user.role != 'student':
        return jsonify({'success': False, 'error': 'Access denied'}), 403
        
    result = check_student_early_verification(current_user.id, session_id)
    return jsonify({'success': True, 'data': result})


@webauthn_bp.route('/credentials', methods=['GET'])
@login_required
def list_credentials():
    """List registered WebAuthn credentials for current user"""
    creds = WebAuthnCredential.query.filter_by(user_id=current_user.id, is_active=True).all()
    data = [{
        'id': c.id,
        'credential_id': c.credential_id,
        'nickname': c.nickname,
        'created_at': c.created_at.strftime('%Y-%m-%d %H:%M') if c.created_at else None,
        'last_used_at': c.last_used_at.strftime('%Y-%m-%d %H:%M') if c.last_used_at else None
    } for c in creds]
    return jsonify({'success': True, 'credentials': data})


@webauthn_bp.route('/credentials/<cred_id>', methods=['DELETE'])
@login_required
def delete_credential(cred_id):
    """Remove a registered credential"""
    cred = WebAuthnCredential.query.filter_by(id=cred_id, user_id=current_user.id).first()
    if not cred:
        return jsonify({'success': False, 'error': 'Credential not found'}), 404
        
    cred.is_active = False
    db.session.commit()
    return jsonify({'success': True, 'message': 'Credential removed'})


@webauthn_bp.route('/fallback-verify', methods=['POST'])
@login_required
def fallback_verify():
    """
    Accessible fallback for unsupported devices or students without biometrics.
    Validates institutional student credentials and creates early verification record.
    """
    if current_user.role != 'student':
        return jsonify({'success': False, 'error': 'Access denied'}), 403
        
    data = request.get_json() or {}
    session_id = data.get('session_id')
    password = data.get('password')
    
    if not session_id or not password:
        return jsonify({'success': False, 'error': 'session_id and password required'}), 400
        
    if not current_user.check_password(password):
        return jsonify({'success': False, 'error': 'Incorrect password'}), 400
        
    session = ClassSession.query.get(session_id)
    if not session:
        return jsonify({'success': False, 'error': 'Session not found'}), 404
        
    if not session.is_early_verification_open():
        return jsonify({'success': False, 'error': 'Early verification window is not currently open'}), 400
        
    from datetime import timedelta
    from app.models import EarlyBiometricVerification
    
    session_end = session.end_datetime or (datetime.now() + timedelta(hours=1))
    expires_at = session_end + timedelta(minutes=30)
    
    early_rec = EarlyBiometricVerification.query.filter_by(
        student_id=current_user.id,
        session_id=session.id
    ).first()
    
    if not early_rec:
        early_rec = EarlyBiometricVerification(
            student_id=current_user.id,
            session_id=session.id,
            credential_id='fallback_password_verified',
            verified_at=datetime.utcnow(),
            expires_at=expires_at,
            status='valid'
        )
        db.session.add(early_rec)
    else:
        early_rec.verified_at = datetime.utcnow()
        early_rec.expires_at = expires_at
        early_rec.status = 'valid'
        early_rec.credential_id = 'fallback_password_verified'
        
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': 'Identity verified via student credential! When class opens, scan the rotating QR code to mark attendance.',
        'verified_at': early_rec.verified_at.isoformat()
    })
