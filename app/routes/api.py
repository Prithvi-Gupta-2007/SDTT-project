"""
API routes for AJAX endpoints
"""
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime, date
from app import db, socketio
from app.models import (

    User, Student, Professor, ClassInstance, ClassSession, 
    Enrollment, AttendanceRecord, Course, AcademicPeriod, Department
)
from app.utils.qr_generator import QRCodeGenerator
import json

api_bp = Blueprint('api', __name__)


@api_bp.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'OK',
        'timestamp': datetime.utcnow().isoformat(),
        'version': '1.0.0',
        'features': ['qr-generation', 'attendance-tracking', 'real-time-updates', 'role-based-access']
    })


@api_bp.route('/students')
@login_required
def get_students():
    """Get all students (for professor enrollment)"""
    if current_user.role != 'professor':
        return jsonify({'success': False, 'error': 'Access denied'})
    
    students = Student.query.all()
    result = []
    for student in students:
        user = User.query.get(student.user_id)
        if user:
            result.append({
                'id': student.user_id,
                'student_id': student.student_id,
                'name': user.full_name,
                'email': user.email,
                'major': student.major
            })
    
    return jsonify({'success': True, 'data': result})


@api_bp.route('/courses')
@login_required
def get_courses():
    """Get all courses"""
    courses = Course.query.filter_by(is_active=True).all()
    result = [{
        'id': c.id,
        'code': c.code,
        'name': c.name,
        'credits': c.credits
    } for c in courses]
    
    return jsonify({'success': True, 'data': result})


@api_bp.route('/departments')
@login_required
def get_departments():
    """Get all departments"""
    departments = Department.query.filter_by(is_active=True).all()
    result = [{
        'id': d.id,
        'code': d.code,
        'name': d.name
    } for d in departments]
    
    return jsonify({'success': True, 'data': result})


@api_bp.route('/academic-periods')
@login_required
def get_academic_periods():
    """Get all academic periods"""
    periods = AcademicPeriod.query.filter_by(is_active=True).order_by(AcademicPeriod.year.desc()).all()
    result = [{
        'id': p.id,
        'name': p.name,
        'year': p.year,
        'semester': p.semester,
        'is_current': p.is_current
    } for p in periods]
    
    return jsonify({'success': True, 'data': result})


@api_bp.route('/attendance/scan', methods=['POST'])
@login_required
def scan_attendance():
    """Process QR code scan for attendance"""
    if current_user.role != 'student':
        return jsonify({'success': False, 'error': 'Only students can scan attendance'})
    
    data = request.get_json()
    qr_data = data.get('qr_data')
    
    if not qr_data:
        return jsonify({'success': False, 'error': 'No QR data provided'})
    
    # Validate QR code
    if isinstance(qr_data, str):
        try:
            qr_data = json.loads(qr_data)
        except:
            return jsonify({'success': False, 'error': 'Invalid QR data format'})
    
    validation = QRCodeGenerator.validate_qr(qr_data)
    
    if not validation['isValid']:
        return jsonify({'success': False, 'error': validation['error']})
    
    session_id = validation['sessionId']
    
    # Get session
    session = ClassSession.query.get(session_id)
    if not session:
        return jsonify({'success': False, 'error': 'Session not found'})
    
    if session.status != 'active':
        return jsonify({'success': False, 'error': 'Session is not active'})
    
    # Check enrollment
    enrollment = Enrollment.query.filter_by(
        student_id=current_user.id,
        class_instance_id=session.class_instance_id,
        status='active'
    ).first()
    
    if not enrollment:
        return jsonify({'success': False, 'error': 'You are not enrolled in this class'})
    
    # Check if already marked
    existing = AttendanceRecord.query.filter_by(
        session_id=session_id,
        student_id=current_user.id
    ).first()
    
    if existing:
        return jsonify({
            'success': False,
            'error': 'Attendance already marked',
            'attendance': {
                'status': existing.status,
                'scanned_at': existing.scanned_at.isoformat()
            }
        })
    
    # Determine if late
    # Determine if late
    session_start = datetime.combine(session.date, datetime.strptime(session.start_time, '%H:%M').time())
    now = datetime.now()
    minutes_late = int((now - session_start).total_seconds() / 60)
    is_late = minutes_late > 5
    
    # Determine verification method (check 5-minute early biometric verification)
    from app.utils.webauthn_service import check_student_early_verification, consume_early_verification
    early_bio_check = check_student_early_verification(current_user.id, session_id)
    if early_bio_check.get('verified'):
        verification_method = 'webauthn_early'
        consume_early_verification(current_user.id, session_id)
    else:
        verification_method = data.get('verification_method', 'qr_standard')
    
    # Create attendance record
    status = 'late' if is_late else 'present'
    
    import uuid
    attendance = AttendanceRecord(
        id=str(uuid.uuid4()),
        session_id=session_id,
        student_id=current_user.id,
        scanned_at=now,
        status=status,
        minutes_late=minutes_late if is_late else 0,
        verification_method=verification_method,
        device_fingerprint=request.headers.get('User-Agent', 'unknown'),
        ip_address=request.remote_addr
    )
    
    db.session.add(attendance)
    session.attendance_count += 1
    db.session.commit()
    
    # Emit socket event for real-time live attendance update
    student = Student.query.filter_by(user_id=current_user.id).first()
    socketio.emit('attendance_update', {
        'sessionId': session_id,
        'studentId': current_user.id,
        'status': status,
        'scanned_at': now.isoformat(),
        'attendanceCount': session.attendance_count,
        'newRecord': {
            'id': attendance.id,
            'name': current_user.full_name,
            'student_id': student.student_id if student else current_user.email,
            'status': status,
            'verification_method': verification_method,
            'time': now.strftime('%H:%M:%S')
        }
    }, room=f'session-{session_id}')
    
    course = session.class_instance.course
    
    return jsonify({
        'success': True,
        'message': f'Attendance marked: {status.capitalize()} (Verified: {verification_method})',
        'attendance': {
            'status': status,
            'verification_method': verification_method,
            'scanned_at': now.isoformat(),
            'minutes_late': minutes_late if is_late else 0,
            'class': {
                'code': course.code,
                'name': course.name
            }
        }
    })


@api_bp.route('/sessions/<session_id>/qr')
@login_required
def get_session_qr(session_id):
    """Get QR code for a session"""
    if current_user.role != 'professor':
        return jsonify({'success': False, 'error': 'Access denied'})
    
    session = ClassSession.query.get(session_id)
    if not session:
        return jsonify({'success': False, 'error': 'Session not found'})
    
    if session.class_instance.professor_id != current_user.id:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    if session.status != 'active':
        return jsonify({'success': False, 'error': 'Session is not active'})
    
    base_url = request.host_url.rstrip('/')
    qr_data = QRCodeGenerator.generate_secure_qr(session_id, base_url)
    
    return jsonify({
        'success': True,
        'qr_code': qr_data['qr_code'],
        'expires_at': qr_data['expires_at']
    })


@api_bp.route('/sessions/<session_id>/attendance')
@login_required
def get_session_attendance(session_id):
    """Get attendance for a session"""
    session = ClassSession.query.get(session_id)
    if not session:
        return jsonify({'success': False, 'error': 'Session not found'})
    
    ci = session.class_instance
    
    # Check access
    if current_user.role == 'professor' and ci.professor_id != current_user.id:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    records = AttendanceRecord.query.filter_by(session_id=session_id).all()
    
    result = []
    for record in records:
        student = Student.query.filter_by(user_id=record.student_id).first()
        user = User.query.get(record.student_id)
        
        if student and user:
            result.append({
                'id': record.id,
                'student_id': student.student_id,
                'name': user.full_name,
                'status': record.status,
                'verification_method': record.verification_method or 'qr_standard',
                'scanned_at': record.scanned_at.isoformat() if record.scanned_at else '',
                'minutes_late': record.minutes_late,
                'reason': record.status_change_reason or ''
            })
    
    # Get stats
    enrolled = Enrollment.query.filter_by(
        class_instance_id=ci.id,
        status='active'
    ).count()
    
    present = sum(1 for r in result if r['status'] == 'present')
    late = sum(1 for r in result if r['status'] == 'late')

    excused = sum(1 for r in result if r['status'] == 'excused')
    absent = enrolled - len(result)
    
    return jsonify({
        'success': True,
        'attendance': result,
        'stats': {
            'present': present,
            'late': late,
            'absent': absent,
            'excused': excused,
            'total': enrolled
        }
    })


@api_bp.route('/student/today-stats')
@login_required
def student_today_stats():
    """Get student's today attendance stats"""
    if current_user.role != 'student':
        return jsonify({'success': False, 'error': 'Access denied'})
    
    today = date.today()
    
    records = db.session.query(AttendanceRecord).join(ClassSession).filter(
        AttendanceRecord.student_id == current_user.id,
        ClassSession.date == today
    ).all()
    
    stats = {
        'scans_today': len(records),
        'present': sum(1 for r in records if r.status == 'present'),
        'late': sum(1 for r in records if r.status == 'late'),
        'absent': sum(1 for r in records if r.status == 'absent'),
        'excused': sum(1 for r in records if r.status == 'excused')
    }
    
    return jsonify({'success': True, 'stats': stats})
