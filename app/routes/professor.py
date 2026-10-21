"""
Professor routes for dashboard, classes, sessions, and attendance management
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, make_response, send_file, current_app
from flask_login import login_required, current_user
from datetime import datetime, date, timedelta
from app import db, socketio
from app.models import (
    User, Professor, Student, ClassInstance, ClassSession, Enrollment, 
    AttendanceRecord, Course, AcademicPeriod, Notification, AttendanceAuditLog
)

from app.utils.qr_generator import QRCodeGenerator
from app.utils.session_generator import generate_sessions_for_class
from app.utils.reports import (
    generate_attendance_report_data, export_to_excel, export_to_csv,
    generate_class_summary, get_low_attendance_students
)
from app.utils.bulk_enrollment import (
    parse_enrollment_csv, bulk_enroll_students, generate_enrollment_template,
    validate_class_enrollment
)
from app.utils.email_service import send_low_attendance_alert, send_session_reminder
import json
import uuid
import secrets
from io import BytesIO

professor_bp = Blueprint('professor', __name__)


def professor_required(f):
    """Decorator to ensure user is a professor"""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'professor':
            flash('Access denied. Professor account required.', 'error')
            return redirect(url_for('auth.professor_login'))
        return f(*args, **kwargs)
    return decorated_function


@professor_bp.route('/dashboard')
@login_required
@professor_required
def dashboard():
    """Professor dashboard"""
    professor = Professor.query.filter_by(user_id=current_user.id).first()
    
    # Get professor's classes
    class_instances = ClassInstance.query.filter_by(
        professor_id=current_user.id,
        is_active=True
    ).all()
    
    today = date.today()
    today_str = today.strftime('%A')
    
    # Stats
    total_students = 0
    active_sessions = 0
    total_attendance_rate = 0
    class_count = 0
    
    classes = []
    today_classes = []
    active_session_list = []
    
    for ci in class_instances:
        course = ci.course
        days_list = ci.days_list
        meets_today = today_str in days_list
        
        # Count enrolled students
        enrolled = Enrollment.query.filter_by(
            class_instance_id=ci.id,
            status='active'
        ).count()
        total_students += enrolled
        
        # Get sessions
        sessions = ClassSession.query.filter_by(class_instance_id=ci.id).all()
        active_count = sum(1 for s in sessions if s.status == 'active')
        active_sessions += active_count
        
        # Calculate attendance rate
        completed_sessions = [s for s in sessions if s.status == 'completed']
        if completed_sessions:
            total_possible = len(completed_sessions) * enrolled
            total_attended = sum(
                AttendanceRecord.query.filter_by(session_id=s.id).filter(
                    AttendanceRecord.status.in_(['present', 'late', 'excused'])
                ).count() for s in completed_sessions
            )
            rate = round((total_attended / total_possible * 100)) if total_possible > 0 else 0
            total_attendance_rate += rate
            class_count += 1
        
        # Get today's session
        today_session = ClassSession.query.filter_by(
            class_instance_id=ci.id,
            date=today
        ).first()
        
        class_data = {
            'id': ci.id,
            'code': course.code,
            'name': course.name,
            'room': ci.room_location,
            'schedule': f"{ci.start_time}-{ci.end_time}",
            'days': days_list,
            'enrolled_students': enrolled,
            'student_count': enrolled,
            'max_students': ci.max_students,
            'meets_today': meets_today,
            'today_session': today_session,
            'has_active_session': today_session and today_session.status == 'active' if today_session else False,
            'active_session_id': today_session.id if today_session and today_session.status == 'active' else None
        }
        
        classes.append(class_data)
        
        if meets_today:
            today_classes.append(class_data)
        
        # Track active sessions
        if class_data['has_active_session']:
            attendance_count = AttendanceRecord.query.filter_by(
                session_id=today_session.id
            ).filter(AttendanceRecord.status.in_(['present', 'late', 'excused'])).count()
            
            active_session_list.append({
                'id': today_session.id,
                'class_code': course.code,
                'class_name': course.name,
                'present_count': attendance_count,
                'total_students': enrolled,
                'qr_expires_at': today_session.qr_expires_at.isoformat() if today_session.qr_expires_at else None
            })
    
    avg_attendance = round(total_attendance_rate / class_count) if class_count > 0 else 0
    
    stats = {
        'total_classes': len(class_instances),
        'total_students': total_students,
        'active_sessions': active_sessions,
        'average_attendance': avg_attendance
    }
    
    return render_template('professor/dashboard.html',
        professor=professor,
        stats=stats,
        classes=classes,
        today_classes=today_classes,
        active_sessions=active_session_list,
        current_time=datetime.now()
    )


@professor_bp.route('/classes')
@login_required
@professor_required
def classes():
    """Professor classes list"""
    class_instances = ClassInstance.query.filter_by(
        professor_id=current_user.id
    ).order_by(ClassInstance.created_at.desc()).all()
    
    classes = []
    for ci in class_instances:
        course = ci.course
        period = ci.academic_period
        
        enrolled = Enrollment.query.filter_by(
            class_instance_id=ci.id,
            status='active'
        ).count()
        
        sessions = ClassSession.query.filter_by(class_instance_id=ci.id).all()
        total_sessions = len(sessions)
        completed_sessions = sum(1 for s in sessions if s.status == 'completed')
        
        classes.append({
            'id': ci.id,
            'code': course.code,
            'name': course.name,
            'class_code': ci.class_code,
            'room': ci.room_location,
            'schedule': f"{', '.join(ci.days_list)} {ci.start_time}-{ci.end_time}",
            'period': period.name if period else 'Unknown',
            'enrolled_students': enrolled,
            'max_students': ci.max_students,
            'total_sessions': total_sessions,
            'completed_sessions': completed_sessions,
            'is_active': ci.is_active
        })
    
    return render_template('professor/classes.html', classes=classes)


@professor_bp.route('/classes/create', methods=['GET', 'POST'])
@login_required
@professor_required
def create_class():
    """Create a new class"""
    if request.method == 'POST':
        try:
            course_id = request.form.get('course_id')
            period_id = request.form.get('academic_period_id')
            days = request.form.getlist('days_of_week')
            start_time = request.form.get('start_time')
            end_time = request.form.get('end_time')
            first_class_date = request.form.get('first_class_date')
            last_class_date = request.form.get('last_class_date')
            room_location = request.form.get('room_location')
            max_students = int(request.form.get('max_students', 30))
            
            # Get course for class code
            course = Course.query.get(course_id)
            if not course:
                flash('Invalid course selected.', 'error')
                return redirect(url_for('professor.create_class'))
            
            # Generate class code
            class_code = f"{course.code.replace('-', '')}-{secrets.token_hex(3).upper()}"
            
            # Calculate enrollment deadline (2 weeks from first class)
            first_date = datetime.strptime(first_class_date, '%Y-%m-%d').date()
            enrollment_deadline = first_date + timedelta(days=14)
            
            # Get next section number
            existing_sections = ClassInstance.query.filter_by(
                course_id=course_id,
                academic_period_id=period_id
            ).count()
            section_number = existing_sections + 1
            
            # Create class instance
            if not last_class_date:
                flash('Last class date is required.', 'error')
                return redirect(url_for('professor.create_class'))
            class_instance = ClassInstance(
                id=str(uuid.uuid4()),
                course_id=course_id,
                professor_id=current_user.id,
                academic_period_id=period_id,
                section_number=section_number,
                class_code=class_code,
                days_of_week=json.dumps(days),
                start_time=start_time,
                end_time=end_time,
                first_class_date=first_date,
                last_class_date=datetime.strptime(last_class_date, '%Y-%m-%d').date(),
                room_location=room_location,
                max_students=max_students,
                enrollment_deadline=enrollment_deadline
            )
            
            db.session.add(class_instance)
            db.session.commit()
            
            # Generate sessions
            generate_sessions_for_class(class_instance)
            
            flash(f'Class {class_code} created successfully with sessions generated!', 'success')
            return redirect(url_for('professor.class_detail', class_id=class_instance.id))
            
        except (ValueError, KeyError) as e:
            db.session.rollback()
            flash(f'Error creating class: {str(e)}', 'error')
    
    # Get available courses and periods for the form
    courses = Course.query.filter_by(is_active=True).all()
    periods = AcademicPeriod.query.filter_by(is_active=True).order_by(AcademicPeriod.year.desc()).all()
    
    return render_template('professor/create_class.html', courses=courses, periods=periods)


@professor_bp.route('/classes/<class_id>')
@login_required
@professor_required
def class_detail(class_id):
    """Class detail page"""
    class_instance = ClassInstance.query.get_or_404(class_id)
    
    if class_instance.professor_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('professor.classes'))
    
    course = class_instance.course
    period = class_instance.academic_period
    
    # Get enrolled students
    enrollments = Enrollment.query.filter_by(
        class_instance_id=class_id,
        status='active'
    ).all()
    
    students = []
    for enrollment in enrollments:
        student = Student.query.filter_by(user_id=enrollment.student_id).first()
        user = User.query.get(enrollment.student_id)
        
        if student and user:
            # Get attendance stats
            total = ClassSession.query.filter_by(
                class_instance_id=class_id,
                status='completed'
            ).count()
            
            attended = AttendanceRecord.query.filter(
                AttendanceRecord.student_id == enrollment.student_id,
                AttendanceRecord.session_id.in_([
                    s.id for s in ClassSession.query.filter_by(class_instance_id=class_id).all()
                ]),
                AttendanceRecord.status.in_(['present', 'late', 'excused'])
            ).count()
            
            rate = round((attended / total * 100)) if total > 0 else 0
            
            students.append({
                'id': student.user_id,
                'student_id': student.student_id,
                'name': user.full_name,
                'email': user.email,
                'attendance_rate': rate,
                'attended': attended,
                'total': total
            })
    
    # Get sessions
    sessions = ClassSession.query.filter_by(
        class_instance_id=class_id
    ).order_by(ClassSession.date).all()
    
    today = date.today()
    past_sessions = []
    upcoming_sessions = []
    active_session = None
    
    for s in sessions:
        # Add attendance count
        s.attendance_count = AttendanceRecord.query.filter_by(
            session_id=s.id
        ).filter(AttendanceRecord.status.in_(['present', 'late', 'excused'])).count()
        
        if s.status == 'active':
            active_session = s
        
        if s.date < today:
            past_sessions.append(s)
        else:
            upcoming_sessions.append(s)
    
    # Calculate average attendance
    completed_sessions = [s for s in sessions if s.status == 'completed']
    if completed_sessions and len(students) > 0:
        total_possible = len(completed_sessions) * len(students)
        total_attended = sum(
            AttendanceRecord.query.filter_by(session_id=s.id).filter(
                AttendanceRecord.status.in_(['present', 'late', 'excused'])
            ).count() for s in completed_sessions
        )
        avg_attendance = round((total_attended / total_possible * 100)) if total_possible > 0 else 0
    else:
        avg_attendance = 0
    
    stats = {
        'average_attendance': avg_attendance
    }
    
    class_data = {
        'id': class_instance.id,
        'code': course.code,
        'name': course.name,
        'class_code': class_instance.class_code,
        'description': course.description,
        'credits': course.credits,
        'room': class_instance.room_location,
        'schedule': f"{', '.join(class_instance.days_list)} {class_instance.start_time}-{class_instance.end_time}",
        'period': period.name if period else 'Unknown',
        'max_students': class_instance.max_students,
        'enrolled': len(students)
    }
    
    return render_template('professor/class_detail.html',
        class_data=class_data,
        students=students,
        past_sessions=past_sessions,
        upcoming_sessions=upcoming_sessions,
        active_session=active_session,
        stats=stats
    )


@professor_bp.route('/classes/<class_id>/enroll', methods=['POST'])
@login_required
@professor_required
def enroll_students(class_id):
    """Enroll students in a class"""
    class_instance = ClassInstance.query.get_or_404(class_id)
    
    if class_instance.professor_id != current_user.id:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    student_ids = request.form.getlist('student_ids')
    
    enrolled_count = 0
    for student_id in student_ids:
        # Check if already enrolled
        existing = Enrollment.query.filter_by(
            student_id=student_id,
            class_instance_id=class_id
        ).first()
        
        if not existing:
            enrollment = Enrollment(
                id=str(uuid.uuid4()),
                student_id=student_id,
                class_instance_id=class_id,
                enrolled_by=current_user.id,
                enrollment_method='manual'
            )
            db.session.add(enrollment)
            class_instance.current_enrollment += 1
            enrolled_count += 1
    
    db.session.commit()
    flash(f'{enrolled_count} student(s) enrolled successfully.', 'success')
    return redirect(url_for('professor.class_detail', class_id=class_id))


@professor_bp.route('/classes/<class_id>/add-student', methods=['POST'])
@login_required
@professor_required
def add_enrollment(class_id):
    """Add a single student to a class by ID or email"""
    class_instance = ClassInstance.query.get_or_404(class_id)
    
    if class_instance.professor_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('professor.class_detail', class_id=class_id))
    
    identifier = request.form.get('student_identifier', '').strip()
    
    if not identifier:
        flash('Please provide a student ID or email.', 'error')
        return redirect(url_for('professor.class_detail', class_id=class_id))
    
    # Find student by ID or email
    student = None
    if '@' in identifier:
        # Search by email
        user = User.query.filter_by(email=identifier, role='student').first()
        if user:
            student = Student.query.filter_by(user_id=user.id).first()
    else:
        # Search by student ID
        student = Student.query.filter_by(student_id=identifier).first()
    
    if not student:
        flash('Student not found.', 'error')
        return redirect(url_for('professor.class_detail', class_id=class_id))
    
    # Check if already enrolled
    existing = Enrollment.query.filter_by(
        student_id=student.user_id,
        class_instance_id=class_id
    ).first()
    
    if existing:
        if existing.status == 'dropped':
            existing.status = 'active'
            existing.updated_at = datetime.utcnow()
            db.session.commit()
            flash('Student re-enrolled successfully.', 'success')
        else:
            flash('Student is already enrolled in this class.', 'warning')
    else:
        # Check class capacity
        current_count = Enrollment.query.filter_by(
            class_instance_id=class_id,
            status='active'
        ).count()
        
        if current_count >= class_instance.max_students:
            flash('Class is full.', 'error')
            return redirect(url_for('professor.class_detail', class_id=class_id))
        
        enrollment = Enrollment(
            id=str(uuid.uuid4()),
            student_id=student.user_id,
            class_instance_id=class_id,
            enrolled_by=current_user.id,
            enrollment_method='manual'
        )
        db.session.add(enrollment)
        class_instance.current_enrollment += 1
        db.session.commit()
        flash('Student enrolled successfully.', 'success')
    
    return redirect(url_for('professor.class_detail', class_id=class_id))


@professor_bp.route('/classes/<class_id>/students/<student_id>/remove', methods=['POST'])
@login_required
@professor_required
def remove_student(class_id, student_id):
    """Remove a student from a class"""
    class_instance = ClassInstance.query.get_or_404(class_id)
    
    if class_instance.professor_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('professor.class_detail', class_id=class_id))
    
    enrollment = Enrollment.query.filter_by(
        student_id=student_id,
        class_instance_id=class_id
    ).first()
    
    if enrollment:
        enrollment.status = 'dropped'
        enrollment.updated_at = datetime.utcnow()
        class_instance.current_enrollment -= 1
        db.session.commit()
        flash('Student removed from class.', 'success')
    else:
        flash('Enrollment not found.', 'error')
    
    return redirect(url_for('professor.class_detail', class_id=class_id))


@professor_bp.route('/sessions')
@login_required
@professor_required
def sessions():
    """All sessions list"""
    class_instances = ClassInstance.query.filter_by(professor_id=current_user.id).all()
    class_ids = [ci.id for ci in class_instances]
    
    all_sessions = ClassSession.query.filter(
        ClassSession.class_instance_id.in_(class_ids)
    ).order_by(ClassSession.date.desc()).all()
    
    sessions_list = []
    active_sessions_list = []
    
    for session in all_sessions:
        ci = session.class_instance
        course = ci.course
        
        attendance_count = AttendanceRecord.query.filter_by(
            session_id=session.id
        ).filter(AttendanceRecord.status.in_(['present', 'late', 'excused'])).count()
        
        enrolled = Enrollment.query.filter_by(
            class_instance_id=ci.id,
            status='active'
        ).count()
        
        session_data = {
            'id': session.id,
            'session_number': session.session_number,
            'class_code': course.code,
            'class_name': course.name,
            'date': session.date,
            'start_time': session.start_time,
            'end_time': session.end_time,
            'room': session.room_location,
            'status': session.status,
            'attendance_count': attendance_count,
            'total_students': enrolled,
            'created_at': session.created_at
        }
        
        sessions_list.append(session_data)
        
        if session.status == 'active':
            active_sessions_list.append(session_data)
    
    return render_template('professor/sessions.html', 
                         sessions=sessions_list,
                         active_sessions=active_sessions_list)


@professor_bp.route('/sessions/<session_id>')
@login_required
@professor_required
def session_detail(session_id):
    """Session detail page"""
    session = ClassSession.query.get_or_404(session_id)
    ci = session.class_instance
    
    if ci.professor_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('professor.sessions'))
    
    course = ci.course
    
    # Get attendance records
    records = AttendanceRecord.query.filter_by(session_id=session_id).all()
    
    attendance_list = []
    for record in records:
        student = Student.query.filter_by(user_id=record.student_id).first()
        user = User.query.get(record.student_id)
        
        if student and user:
            attendance_list.append({
                'id': record.id,
                'student_id': student.student_id,
                'name': user.full_name,
                'email': user.email,
                'status': record.status,
                'verification_method': record.verification_method or 'qr_standard',
                'reason': record.status_change_reason or '',
                'scanned_at': record.scanned_at,
                'minutes_late': record.minutes_late
            })

    
    # Get enrolled students who haven't attended
    enrolled = Enrollment.query.filter_by(
        class_instance_id=ci.id,
        status='active'
    ).all()
    
    attended_ids = [r.student_id for r in records]
    absent_list = []
    
    for enrollment in enrolled:
        if enrollment.student_id not in attended_ids:
            student = Student.query.filter_by(user_id=enrollment.student_id).first()
            user = User.query.get(enrollment.student_id)
            if student and user:
                absent_list.append({
                    'student_id': student.student_id,
                    'name': user.full_name,
                    'email': user.email
                })
    
    session_data = {
        'id': session.id,
        'class_code': course.code,
        'class_name': course.name,
        'class_instance_id': ci.id,
        'date': session.date,
        'start_time': session.start_time,
        'end_time': session.end_time,
        'room': session.room_location,
        'status': session.status,
        'is_active': session.is_active,
        'qr_expires_at': session.qr_expires_at,
        'session_number': session.session_number
    }
    
    stats = {
        'present': sum(1 for a in attendance_list if a['status'] == 'present'),
        'late': sum(1 for a in attendance_list if a['status'] == 'late'),
        'absent': len(absent_list),
        'excused': sum(1 for a in attendance_list if a['status'] == 'excused'),
        'total': len(enrolled),
        'rate': round(((len(attendance_list)) / len(enrolled) * 100)) if enrolled else 0
    }
    
    # Generate QR code if session is active
    qr_image = None
    if session.status == 'active':
        base_url = request.host_url.rstrip('/')
        qr_data = QRCodeGenerator.generate_secure_qr(session_id, base_url)
        qr_image = qr_data['qr_code']
    
    return render_template('professor/session_detail.html',
        session=session_data,
        stats=stats,
        attendance_list=attendance_list,
        absent_list=absent_list,
        total_students=len(enrolled),
        attendance_records=attendance_list,
        qr_image=qr_image,
        qr_rotation_interval=current_app.config['QR_ROTATION_INTERVAL']
    )


@professor_bp.route('/sessions/<session_id>/activate', methods=['POST'])
@login_required
@professor_required
def activate_session(session_id):
    """Activate a session and generate QR code"""
    session = ClassSession.query.get_or_404(session_id)
    ci = session.class_instance
    
    if ci.professor_id != current_user.id:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    try:
        # Generate QR code
        base_url = request.host_url.rstrip('/')
        qr_data = QRCodeGenerator.generate_secure_qr(session_id, base_url)
        
        session.status = 'active'
        session.is_active = True
        session.qr_secret = qr_data['secret']
        session.qr_expires_at = datetime.fromisoformat(qr_data['expires_at'])
        session.updated_at = datetime.utcnow()
        
        # Update total enrolled count
        enrolled = Enrollment.query.filter_by(
            class_instance_id=ci.id,
            status='active'
        ).count()
        session.total_enrolled = enrolled
        
        db.session.commit()
        
        # Notify enrolled students
        course = ci.course
        enrollments = Enrollment.query.filter_by(
            class_instance_id=ci.id,
            status='active'
        ).all()
        
        for enrollment in enrollments:
            notification = Notification(
                id=str(uuid.uuid4()),
                user_id=enrollment.student_id,
                type='session_started',
                title='Session Started',
                message=f'{course.code} session is now active. Scan the QR code!',
                link='/student/scan',
                session_id=session_id
            )
            db.session.add(notification)
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'qr_code': qr_data['qr_code'],
            'expires_at': qr_data['expires_at'],
            'session_id': session_id
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})


@professor_bp.route('/sessions/<session_id>/refresh-qr', methods=['POST'])
@login_required
@professor_required
def refresh_qr(session_id):
    """Refresh QR code for active session"""
    session = ClassSession.query.get_or_404(session_id)
    ci = session.class_instance
    
    if ci.professor_id != current_user.id:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    if session.status != 'active':
        return jsonify({'success': False, 'error': 'Session is not active'})
    
    try:
        base_url = request.host_url.rstrip('/')
        qr_data = QRCodeGenerator.generate_secure_qr(session_id, base_url)
        
        session.qr_secret = qr_data['secret']
        session.qr_expires_at = datetime.fromisoformat(qr_data['expires_at'])
        session.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        # Emit socket event for real-time QR update
        socketio.emit('qr_update', {
            'sessionId': session_id,
            'qr_code': qr_data['qr_code'],
            'expires_at': qr_data['expires_at']
        }, room=f'session-{session_id}')
        
        return jsonify({
            'success': True,
            'qr_code': qr_data['qr_code'],
            'expires_at': qr_data['expires_at']
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@professor_bp.route('/sessions/<session_id>/complete', methods=['POST'])
@login_required
@professor_required
def complete_session(session_id):
    """Complete a session and mark absent students"""
    session = ClassSession.query.get_or_404(session_id)
    ci = session.class_instance
    
    if ci.professor_id != current_user.id:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    try:
        # Get all enrolled students
        enrollments = Enrollment.query.filter_by(
            class_instance_id=ci.id,
            status='active'
        ).all()
        
        # Get students who already have attendance
        existing_records = AttendanceRecord.query.filter_by(
            session_id=session_id
        ).all()
        attended_ids = [r.student_id for r in existing_records]
        
        # Mark absent for those who didn't scan
        for enrollment in enrollments:
            if enrollment.student_id not in attended_ids:
                absent_record = AttendanceRecord(
                    id=str(uuid.uuid4()),
                    session_id=session_id,
                    student_id=enrollment.student_id,
                    status='absent',
                    scanned_at=datetime.utcnow()
                )
                db.session.add(absent_record)
        
        # Update session status
        session.status = 'completed'
        session.is_active = False
        session.qr_expires_at = None
        session.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        flash('Session completed. Absent students marked.', 'success')
        return jsonify({'success': True})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})


@professor_bp.route('/sessions/<session_id>/cancel', methods=['POST'])
@login_required
@professor_required
def cancel_session(session_id):
    """Cancel a session"""
    session = ClassSession.query.get_or_404(session_id)
    ci = session.class_instance
    
    if ci.professor_id != current_user.id:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    reason = request.form.get('reason', '')
    
    session.status = 'cancelled'
    session.is_active = False
    session.notes = reason
    session.updated_at = datetime.utcnow()
    
    db.session.commit()
    
    flash('Session cancelled.', 'info')
    return redirect(url_for('professor.sessions'))


@professor_bp.route('/attendance/<record_id>/update', methods=['POST'])
@login_required
@professor_required
def update_attendance(record_id):
    """Update attendance status with audit logging"""
    record = AttendanceRecord.query.get_or_404(record_id)
    session = ClassSession.query.get(record.session_id)
    ci = session.class_instance
    
    if ci.professor_id != current_user.id:
        return jsonify({'success': False, 'error': 'Access denied'}), 403
    
    # Handle either JSON or form submission
    req_data = request.get_json(silent=True) or request.form
    new_status = req_data.get('status')
    reason = req_data.get('reason', '').strip()
    
    if not new_status or new_status not in ['present', 'late', 'absent', 'excused']:
        return jsonify({'success': False, 'error': 'Invalid status'}), 400
        
    if not reason:
        return jsonify({'success': False, 'error': 'A specific reason is required for manual attendance changes.'}), 400
    
    previous_status = record.status
    record.status = new_status
    record.status_changed_by = current_user.id
    record.status_changed_at = datetime.utcnow()
    record.status_change_reason = reason
    record.verification_method = 'manual_override'
    
    # Log audit entry
    audit_log = AttendanceAuditLog(
        attendance_id=record.id,
        session_id=session.id,
        student_id=record.student_id,
        changed_by_user_id=current_user.id,
        previous_status=previous_status,
        new_status=new_status,
        reason=reason,
        ip_address=request.remote_addr
    )
    db.session.add(audit_log)
    db.session.commit()
    
    # Notify live dashboard via Socket.IO
    student = Student.query.filter_by(user_id=record.student_id).first()
    student_user = User.query.get(record.student_id)
    socketio.emit('attendance_update', {
        'sessionId': session.id,
        'studentId': record.student_id,
        'status': new_status,
        'scanned_at': record.scanned_at.isoformat() if record.scanned_at else datetime.utcnow().isoformat(),
        'attendanceCount': session.attendance_count,
        'newRecord': {
            'id': record.id,
            'name': student_user.full_name if student_user else 'Student',
            'student_id': student.student_id if student else 'STU',
            'status': new_status,
            'verification_method': 'manual_override',
            'time': datetime.now().strftime('%H:%M:%S')
        }
    }, room=f'session-{session.id}')
    
    return jsonify({
        'success': True,
        'message': f'Attendance updated to {new_status} with audit log saved.',
        'audit_id': audit_log.id
    })



@professor_bp.route('/profile')
@login_required
@professor_required
def profile():
    """Professor profile page"""
    professor = Professor.query.filter_by(user_id=current_user.id).first()
    return render_template('professor/profile.html', professor=professor)


@professor_bp.route('/profile/update', methods=['POST'])
@login_required
@professor_required
def update_profile():
    """Update professor profile"""
    try:
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        phone = request.form.get('phone', '').strip()
        title = request.form.get('title', '').strip()
        office_location = request.form.get('office_location', '').strip()
        
        if first_name:
            current_user.first_name = first_name
        if last_name:
            current_user.last_name = last_name
        if phone:
            current_user.phone = phone
        
        professor = Professor.query.filter_by(user_id=current_user.id).first()
        if professor:
            if title:
                professor.title = title
            if office_location:
                professor.office_location = office_location
        
        db.session.commit()
        flash('Profile updated successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error updating profile: {str(e)}', 'error')
    
    return redirect(url_for('professor.profile'))


@professor_bp.route('/classes/<class_id>/start-session', methods=['GET', 'POST'])
@login_required
@professor_required
def start_session(class_id):
    """Start a session for today's class"""
    class_instance = ClassInstance.query.get_or_404(class_id)
    
    if class_instance.professor_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('professor.classes'))
    
    today = date.today()
    
    # Find today's session
    session = ClassSession.query.filter_by(
        class_instance_id=class_id,
        date=today
    ).first()
    
    if not session:
        # Create a session for today if it doesn't exist
        session_count = ClassSession.query.filter_by(class_instance_id=class_id).count()
        
        session = ClassSession(
            id=str(uuid.uuid4()),
            class_instance_id=class_id,
            session_number=session_count + 1,
            date=today,
            start_time=class_instance.start_time,
            end_time=class_instance.end_time,
            room_location=class_instance.room_location,
            status='scheduled',
            is_active=False
        )
        db.session.add(session)
        db.session.commit()
    
    if session.status == 'active':
        # Already active, redirect to session detail
        return redirect(url_for('professor.session_detail', session_id=session.id))
    
    # Activate the session
    try:
        base_url = request.host_url.rstrip('/')
        qr_data = QRCodeGenerator.generate_secure_qr(session.id, base_url)
        
        session.status = 'active'
        session.is_active = True
        session.qr_secret = qr_data['secret']
        session.qr_expires_at = datetime.fromisoformat(qr_data['expires_at'])
        session.updated_at = datetime.utcnow()
        
        # Update total enrolled count
        enrolled = Enrollment.query.filter_by(
            class_instance_id=class_id,
            status='active'
        ).count()
        session.total_enrolled = enrolled
        
        db.session.commit()
        
        # Notify enrolled students
        course = class_instance.course
        enrollments = Enrollment.query.filter_by(
            class_instance_id=class_id,
            status='active'
        ).all()
        
        for enrollment in enrollments:
            notification = Notification(
                id=str(uuid.uuid4()),
                user_id=enrollment.student_id,
                type='session_started',
                title='Session Started',
                message=f'{course.code} session is now active. Scan the QR code!',
                link='/student/scan',
                session_id=session.id
            )
            db.session.add(notification)
        
        db.session.commit()
        
        flash('Session started successfully!', 'success')
        return redirect(url_for('professor.session_detail', session_id=session.id))
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error starting session: {str(e)}', 'error')
        return redirect(url_for('professor.class_detail', class_id=class_id))


# ==================== REPORTS & ANALYTICS ====================

@professor_bp.route('/reports')
@login_required
@professor_required
def reports():
    """Attendance reports dashboard"""
    professor = Professor.query.filter_by(user_id=current_user.id).first()
    
    # Get professor's classes
    class_instances = ClassInstance.query.filter_by(
        professor_id=current_user.id,
        is_active=True
    ).all()
    
    return render_template('professor/reports.html',
        professor=professor,
        classes=class_instances
    )


@professor_bp.route('/reports/export/<class_id>')
@login_required
@professor_required
def export_report(class_id):
    """Export attendance report"""
    # Verify professor owns this class
    class_instance = ClassInstance.query.filter_by(
        id=class_id,
        professor_id=current_user.id
    ).first()
    
    if not class_instance:
        flash('Class not found', 'error')
        return redirect(url_for('professor.reports'))
    
    # Get filter parameters
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    format_type = request.args.get('format', 'excel')  # 'excel' or 'csv'
    
    # Parse dates
    if start_date:
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
    if end_date:
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
    
    # Generate report data
    data = generate_attendance_report_data(
        class_id=class_id,
        start_date=start_date,
        end_date=end_date
    )
    
    if not data:
        flash('No attendance records found for the selected filters', 'warning')
        return redirect(url_for('professor.reports'))
    
    # Generate filename
    filename = f"attendance_{class_instance.class_code}_{datetime.now().strftime('%Y%m%d')}"
    
    # Export based on format
    if format_type == 'csv':
        return export_to_csv(data, f"{filename}.csv")
    else:
        return export_to_excel(data, f"{filename}.xlsx")


@professor_bp.route('/reports/export-all')
@login_required
@professor_required
def export_all_reports():
    """Export attendance for all classes taught by this professor"""
    format_type = request.args.get('format', 'csv')
    class_instances = ClassInstance.query.filter_by(professor_id=current_user.id).all()
    all_data = []
    for ci in class_instances:
        data = generate_attendance_report_data(class_id=ci.id)
        all_data.extend(data)
        
    if not all_data:
        flash('No attendance records found across classes', 'info')
        return redirect(url_for('professor.reports'))
        
    filename = f"all_classes_attendance_{datetime.now().strftime('%Y%m%d')}"
    if format_type == 'csv':
        return export_to_csv(all_data, f"{filename}.csv")
    return export_to_excel(all_data, f"{filename}.xlsx")



@professor_bp.route('/reports/class/<class_id>')
@login_required
@professor_required
def class_report(class_id):
    """Detailed class attendance report"""
    # Verify professor owns this class
    class_instance = ClassInstance.query.filter_by(
        id=class_id,
        professor_id=current_user.id
    ).first()
    
    if not class_instance:
        flash('Class not found', 'error')
        return redirect(url_for('professor.reports'))
    
    # Generate summary
    summary = generate_class_summary(class_id)
    
    # Get low attendance students
    low_attendance = get_low_attendance_students(class_id, threshold=75)
    
    return render_template('professor/class_report.html',
        class_instance=class_instance,
        summary=summary,
        low_attendance=low_attendance
    )


@professor_bp.route('/reports/analytics')
@login_required
@professor_required
def analytics():
    """Analytics dashboard with charts"""
    professor = Professor.query.filter_by(user_id=current_user.id).first()
    
    # Get all professor's classes
    class_instances = ClassInstance.query.filter_by(
        professor_id=current_user.id,
        is_active=True
    ).all()
    
    # Prepare analytics data
    analytics_data = []
    for class_inst in class_instances:
        summary = generate_class_summary(class_inst.id)
        if summary:
            analytics_data.append({
                'class_code': class_inst.class_code,
                'course_name': class_inst.course.name,
                'total_sessions': summary['total_sessions'],
                'total_students': summary['total_students'],
                'average_attendance': summary['average_attendance']
            })
    
    return render_template('professor/analytics.html',
        professor=professor,
        analytics_data=analytics_data,
        classes=class_instances
    )


# ==================== BULK ENROLLMENT ====================

@professor_bp.route('/classes/<class_id>/bulk-enroll', methods=['GET', 'POST'])
@login_required
@professor_required
def bulk_enroll(class_id):
    """Bulk student enrollment"""
    # Verify professor owns this class
    class_instance = ClassInstance.query.filter_by(
        id=class_id,
        professor_id=current_user.id
    ).first()
    
    if not class_instance:
        flash('Class not found', 'error')
        return redirect(url_for('professor.classes'))
    
    if request.method == 'POST':
        # Check if file was uploaded
        if 'csv_file' not in request.files:
            flash('No file uploaded', 'error')
            return redirect(request.url)
        
        file = request.files['csv_file']
        
        if file.filename == '':
            flash('No file selected', 'error')
            return redirect(request.url)
        
        if not file.filename.endswith('.csv'):
            flash('File must be a CSV file', 'error')
            return redirect(request.url)
        
        try:
            # Read and parse CSV
            file_content = file.read().decode('utf-8')
            students_data, parse_errors = parse_enrollment_csv(file_content)
            
            if parse_errors:
                for error in parse_errors:
                    flash(error, 'error')
                return redirect(request.url)
            
            if not students_data:
                flash('No valid student data found in CSV', 'error')
                return redirect(request.url)
            
            # Enroll students
            results = bulk_enroll_students(class_id, students_data, current_user.id)
            
            # Show results
            if results['success']:
                flash(f"Successfully enrolled {len(results['success'])} students", 'success')
            
            if results['created_accounts']:
                flash(f"Created {len(results['created_accounts'])} new student accounts (default password: student123)", 'info')
            
            if results['already_enrolled']:
                flash(f"{len(results['already_enrolled'])} students were already enrolled", 'warning')
            
            if results['errors']:
                for error in results['errors'][:5]:  # Show first 5 errors
                    flash(error, 'error')
                if len(results['errors']) > 5:
                    flash(f"...and {len(results['errors']) - 5} more errors", 'error')
            
            return redirect(url_for('professor.class_detail', class_id=class_id))
            
        except Exception as e:
            flash(f'Error processing file: {str(e)}', 'error')
            return redirect(request.url)
    
    # GET request - show form
    enrollment_info = validate_class_enrollment(class_id)
    
    return render_template('professor/bulk_enroll.html',
        class_instance=class_instance,
        enrollment_info=enrollment_info
    )


@professor_bp.route('/download-enrollment-template')
@login_required
@professor_required
def download_enrollment_template():
    """Download CSV template for bulk enrollment"""
    template_content = generate_enrollment_template()
    
    # Create response
    output = BytesIO()
    output.write(template_content.encode('utf-8'))
    output.seek(0)
    
    return send_file(
        output,
        mimetype='text/csv',
        as_attachment=True,
        download_name='enrollment_template.csv'
    )


# ==================== EMAIL NOTIFICATIONS ====================

@professor_bp.route('/send-low-attendance-alerts/<class_id>', methods=['POST'])
@login_required
@professor_required
def send_alerts(class_id):
    """Send low attendance alerts to students"""
    from flask import current_app
    
    if not current_app.config.get('ENABLE_EMAIL_NOTIFICATIONS', False):
        flash('Email notifications are not enabled', 'warning')
        return redirect(url_for('professor.class_report', class_id=class_id))
    
    # Verify professor owns this class
    class_instance = ClassInstance.query.filter_by(
        id=class_id,
        professor_id=current_user.id
    ).first()
    
    if not class_instance:
        flash('Class not found', 'error')
        return redirect(url_for('professor.reports'))
    
    # Get low attendance students
    low_attendance = get_low_attendance_students(class_id, threshold=75)
    
    if not low_attendance:
        flash('No students with low attendance found', 'info')
        return redirect(url_for('professor.class_report', class_id=class_id))
    
    # Send alerts
    sent_count = 0
    for student in low_attendance:
        try:
            send_low_attendance_alert(
                student['email'],
                student['student_name'],
                class_instance.class_code,
                class_instance.course.name,
                student['attendance_rate']
            )
            sent_count += 1
        except Exception as e:
            print(f"Failed to send alert to {student['email']}: {e}")
    
    flash(f'Sent {sent_count} low attendance alerts', 'success')
    return redirect(url_for('professor.class_report', class_id=class_id))


@professor_bp.route('/timetable')
@login_required
@professor_required
def timetable():
    """Professor weekly teaching timetable"""
    class_instances = ClassInstance.query.filter_by(
        professor_id=current_user.id,
        is_active=True
    ).all()
    
    weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
    schedule_by_day = {day: [] for day in weekdays}
    
    today = date.today()
    today_name = today.strftime('%A')
    
    for ci in class_instances:
        course = ci.course
        days = ci.days_list
        enrolled_count = Enrollment.query.filter_by(class_instance_id=ci.id, status='active').count()
        
        # Check today's session
        today_session = ClassSession.query.filter_by(class_instance_id=ci.id, date=today).first()
        
        for day in days:
            if day in schedule_by_day:
                schedule_by_day[day].append({
                    'class_id': ci.id,
                    'code': course.code,
                    'name': course.name,
                    'section': ci.section_number,
                    'room': ci.room_location or 'Main Hall',
                    'start_time': ci.start_time,
                    'end_time': ci.end_time,
                    'enrolled_count': enrolled_count,
                    'session_id': today_session.id if today_session else None,
                    'session_status': today_session.status if today_session else 'not_scheduled',
                    'is_today': (day == today_name)
                })
                
    for day in weekdays:
        schedule_by_day[day].sort(key=lambda x: x['start_time'])
        
    return render_template('professor/timetable.html',
        schedule_by_day=schedule_by_day,
        today_name=today_name,
        weekdays=weekdays
    )


@professor_bp.route('/take-attendance')
@login_required
@professor_required
def take_attendance():
    """Quick launcher for professor to select a class and launch dynamic QR session"""
    class_instances = ClassInstance.query.filter_by(
        professor_id=current_user.id,
        is_active=True
    ).all()
    
    classes_data = []
    today = date.today()
    
    for ci in class_instances:
        course = ci.course
        enrolled = Enrollment.query.filter_by(class_instance_id=ci.id, status='active').count()
        today_session = ClassSession.query.filter_by(class_instance_id=ci.id, date=today).first()
        
        classes_data.append({
            'id': ci.id,
            'code': course.code,
            'name': course.name,
            'section': ci.section_number,
            'schedule': f"{', '.join(ci.days_list)} {ci.start_time}-{ci.end_time}",
            'room': ci.room_location,
            'enrolled': enrolled,
            'today_session': today_session
        })
        
    return render_template('professor/take_attendance.html', classes=classes_data)


@professor_bp.route('/take-attendance/start', methods=['POST'])
@login_required
@professor_required
def start_attendance_session():
    """Start an attendance session for a class instance and redirect to live QR view"""
    class_id = request.form.get('class_id')
    ci = ClassInstance.query.filter_by(id=class_id, professor_id=current_user.id).first_or_404()
    
    today = date.today()
    
    # Check if a session already exists for today
    session = ClassSession.query.filter_by(class_instance_id=ci.id, date=today).first()
    
    if not session:
        # Create today's session
        session_count = ClassSession.query.filter_by(class_instance_id=ci.id).count()
        session = ClassSession(
            class_instance_id=ci.id,
            session_number=session_count + 1,
            date=today,
            start_time=ci.start_time,
            end_time=ci.end_time,
            room_location=ci.room_location,
            status='scheduled'
        )
        db.session.add(session)
        db.session.flush()
        
    # Activate session using existing QR code generator
    base_url = request.host_url.rstrip('/')
    qr_data = QRCodeGenerator.generate_secure_qr(session.id, base_url)
    
    session.status = 'active'
    session.is_active = True
    session.qr_secret = qr_data['secret']
    session.qr_expires_at = datetime.fromisoformat(qr_data['expires_at'])
    session.updated_at = datetime.utcnow()
    
    enrolled = Enrollment.query.filter_by(class_instance_id=ci.id, status='active').count()
    session.total_enrolled = enrolled
    
    db.session.commit()
    
    flash(f'Attendance session activated for {ci.course.code}! QR Code is rotating.', 'success')
    return redirect(url_for('professor.session_detail', session_id=session.id))


@professor_bp.route('/students')
@login_required
@professor_required
def students_records():
    """Professor student records and search"""
    class_instances = ClassInstance.query.filter_by(professor_id=current_user.id, is_active=True).all()
    class_ids = [ci.id for ci in class_instances]
    
    enrollments = Enrollment.query.filter(Enrollment.class_instance_id.in_(class_ids), Enrollment.status == 'active').all()
    
    student_records = []
    seen_student_ids = set()
    
    for enr in enrollments:
        if enr.student_id in seen_student_ids:
            continue
        seen_student_ids.add(enr.student_id)
        
        student = Student.query.filter_by(user_id=enr.student_id).first()
        user = User.query.get(enr.student_id)
        
        if student and user:
            # Overall attendance across this professor's classes
            total_sessions = ClassSession.query.filter(
                ClassSession.class_instance_id.in_(class_ids),
                ClassSession.status == 'completed'
            ).count()
            
            attended = db.session.query(AttendanceRecord).join(ClassSession).filter(
                ClassSession.class_instance_id.in_(class_ids),
                AttendanceRecord.student_id == user.id,
                AttendanceRecord.status.in_(['present', 'late', 'excused'])
            ).count()
            
            rate = round((attended / total_sessions * 100)) if total_sessions > 0 else 100
            
            student_records.append({
                'id': user.id,
                'student_id': student.student_id,
                'name': user.full_name,
                'email': user.email,
                'major': student.major,
                'attendance_rate': rate,
                'attended': attended,
                'total': total_sessions
            })
            
    return render_template('professor/students.html', students=student_records)


@professor_bp.route('/settings', methods=['GET', 'POST'])
@login_required
@professor_required
def settings():
    """Professor attendance policy and threshold settings"""
    if request.method == 'POST':
        threshold = request.form.get('low_attendance_threshold')
        if threshold:
            try:
                current_app.config['LOW_ATTENDANCE_THRESHOLD'] = int(threshold)
                flash('Attendance settings updated successfully.', 'success')
            except ValueError:
                flash('Invalid threshold value.', 'error')
                
    return render_template('professor/settings.html',
        threshold=current_app.config.get('LOW_ATTENDANCE_THRESHOLD', 75),
        qr_rotation=current_app.config.get('QR_ROTATION_INTERVAL', 30),
        qr_expiry=current_app.config.get('QR_EXPIRY_SECONDS', 90)
    )

