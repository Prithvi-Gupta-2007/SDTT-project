"""
Database models for the Smart Attendance Management System
"""
from datetime import datetime, date
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
import uuid
import json
from app import db, login_manager


def generate_uuid():
    """Generate a UUID string"""
    return str(uuid.uuid4())


class User(UserMixin, db.Model):
    """User model for authentication"""
    __tablename__ = 'users'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'student', 'professor', 'admin'
    phone = db.Column(db.String(20))
    avatar_url = db.Column(db.String(500))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    student_profile = db.relationship('Student', backref='user', uselist=False, cascade='all, delete-orphan')
    professor_profile = db.relationship('Professor', backref='user', uselist=False, cascade='all, delete-orphan')
    notifications = db.relationship('Notification', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    webauthn_credentials = db.relationship('WebAuthnCredential', backref='user', lazy='dynamic', cascade='all, delete-orphan')

    
    def set_password(self, password):
        """Hash and set the password"""
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        """Check if password matches"""
        return check_password_hash(self.password_hash, password)
    
    @property
    def full_name(self):
        """Get full name"""
        return f"{self.first_name} {self.last_name}"
    
    def to_dict(self):
        """Convert to dictionary"""
        data = {
            'id': self.id,
            'email': self.email,
            'first_name': self.first_name,
            'last_name': self.last_name,
            'role': self.role,
            'phone': self.phone,
            'avatar_url': self.avatar_url,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
        if self.role == 'student' and self.student_profile:
            data['student_id'] = self.student_profile.student_id
            data['major'] = self.student_profile.major
        elif self.role == 'professor' and self.professor_profile:
            data['employee_id'] = self.professor_profile.employee_id
            data['title'] = self.professor_profile.title
        return data


@login_manager.user_loader
def load_user(user_id):
    """Load user by ID for Flask-Login"""
    user = User.query.get(user_id)
    if not user:
        try:
            from app.utils.seed_data import seed_initial_data
            seed_initial_data()
            user = User.query.get(user_id)
        except Exception:
            pass
    return user


class Student(db.Model):
    """Student profile model"""
    __tablename__ = 'students'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), unique=True, nullable=False)
    student_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    major = db.Column(db.String(100), default='Undeclared')
    enrollment_year = db.Column(db.Integer, default=lambda: datetime.utcnow().year)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    enrollments = db.relationship('Enrollment', backref='student', lazy='dynamic', cascade='all, delete-orphan')
    attendance_records = db.relationship('AttendanceRecord', backref='student', lazy='dynamic', cascade='all, delete-orphan')


class Professor(db.Model):
    """Professor profile model"""
    __tablename__ = 'professors'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), unique=True, nullable=False)
    employee_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    title = db.Column(db.String(50), default='Professor')
    department_id = db.Column(db.String(36), db.ForeignKey('departments.id'))
    office_location = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    class_instances = db.relationship('ClassInstance', backref='professor', lazy='dynamic')


class Department(db.Model):
    """Department model"""
    __tablename__ = 'departments'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(20), unique=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    courses = db.relationship('Course', backref='department', lazy='dynamic')
    professors = db.relationship('Professor', backref='department', lazy='dynamic')


class AcademicPeriod(db.Model):
    """Academic period model (semesters)"""
    __tablename__ = 'academic_periods'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    name = db.Column(db.String(100), nullable=False)  # 'Fall 2025', 'Spring 2026'
    year = db.Column(db.Integer, nullable=False)
    semester = db.Column(db.String(20), nullable=False)  # 'fall', 'spring', 'summer'
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    is_current = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    class_instances = db.relationship('ClassInstance', backref='academic_period', lazy='dynamic')


class Course(db.Model):
    """Course catalog model"""
    __tablename__ = 'courses'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    code = db.Column(db.String(20), unique=True, nullable=False, index=True)  # 'CSC-105'
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    credits = db.Column(db.Integer, default=3)
    department_id = db.Column(db.String(36), db.ForeignKey('departments.id'))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    class_instances = db.relationship('ClassInstance', backref='course', lazy='dynamic')


class ClassInstance(db.Model):
    """Class instance model (specific offering of a course)"""
    __tablename__ = 'class_instances'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    course_id = db.Column(db.String(36), db.ForeignKey('courses.id'), nullable=False)
    professor_id = db.Column(db.String(36), db.ForeignKey('professors.user_id'), nullable=False)
    academic_period_id = db.Column(db.String(36), db.ForeignKey('academic_periods.id'), nullable=False)
    
    section_number = db.Column(db.Integer, default=1)
    class_code = db.Column(db.String(20), unique=True, nullable=False, index=True)  # 'CSC105-ABC123'
    
    days_of_week = db.Column(db.Text, nullable=False)  # JSON array: ['Monday', 'Wednesday', 'Friday']
    start_time = db.Column(db.String(10), nullable=False)  # '09:00'
    end_time = db.Column(db.String(10), nullable=False)  # '10:30'
    first_class_date = db.Column(db.Date, nullable=False)
    last_class_date = db.Column(db.Date, nullable=False)
    
    room_location = db.Column(db.String(100))
    max_students = db.Column(db.Integer, default=30)
    current_enrollment = db.Column(db.Integer, default=0)
    
    is_active = db.Column(db.Boolean, default=True)
    enrollment_deadline = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    sessions = db.relationship('ClassSession', backref='class_instance', lazy='dynamic', cascade='all, delete-orphan')
    enrollments = db.relationship('Enrollment', backref='class_instance', lazy='dynamic', cascade='all, delete-orphan')
    
    @property
    def days_list(self):
        """Get days as list"""
        try:
            return json.loads(self.days_of_week) if self.days_of_week else []
        except (json.JSONDecodeError, TypeError):
            return []
    
    @days_list.setter
    def days_list(self, value):
        """Set days from list"""
        self.days_of_week = json.dumps(value)


class ClassSession(db.Model):
    """Class session model (individual class meeting)"""
    __tablename__ = 'class_sessions'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    class_instance_id = db.Column(db.String(36), db.ForeignKey('class_instances.id'), nullable=False)
    session_number = db.Column(db.Integer, nullable=False)
    
    date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.String(10), nullable=False)
    end_time = db.Column(db.String(10), nullable=False)
    room_location = db.Column(db.String(100))
    
    status = db.Column(db.String(20), default='scheduled')  # 'scheduled', 'active', 'cancelled', 'completed'
    notes = db.Column(db.Text)
    
    qr_secret = db.Column(db.String(255))
    qr_expires_at = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=False)
    
    attendance_count = db.Column(db.Integer, default=0)
    total_enrolled = db.Column(db.Integer, default=0)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    attendance_records = db.relationship('AttendanceRecord', backref='session', lazy='dynamic', cascade='all, delete-orphan')
    early_verifications = db.relationship('EarlyBiometricVerification', backref='session', lazy='dynamic', cascade='all, delete-orphan')
    audit_logs = db.relationship('AttendanceAuditLog', backref='session', lazy='dynamic', cascade='all, delete-orphan')

    @property
    def start_datetime(self):
        """Get session start as datetime"""
        try:
            return datetime.combine(self.date, datetime.strptime(self.start_time, '%H:%M').time())
        except Exception:
            return None

    @property
    def end_datetime(self):
        """Get session end as datetime"""
        try:
            return datetime.combine(self.date, datetime.strptime(self.end_time, '%H:%M').time())
        except Exception:
            return None

    @property
    def early_window_start(self):
        """Get 5-minute early verification window start time"""
        start = self.start_datetime
        if start:
            from datetime import timedelta
            return start - timedelta(minutes=5)
        return None

    def is_early_verification_open(self, current_dt=None):
        """Check if 5-minute early biometric verification window is open"""
        if current_dt is None:
            current_dt = datetime.now()
        start = self.start_datetime
        if not start:
            return False
        from datetime import timedelta
        window_start = start - timedelta(minutes=5)
        # Early window opens 5 mins before start until end of session or while active
        window_end = self.end_datetime if self.end_datetime else (start + timedelta(hours=1))
        return (window_start <= current_dt <= window_end) or self.status == 'active'


class Enrollment(db.Model):
    """Student enrollment model"""
    __tablename__ = 'enrollments'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    student_id = db.Column(db.String(36), db.ForeignKey('students.user_id'), nullable=False)
    class_instance_id = db.Column(db.String(36), db.ForeignKey('class_instances.id'), nullable=False)
    
    enrollment_date = db.Column(db.Date, default=date.today)
    enrolled_by = db.Column(db.String(36), db.ForeignKey('users.id'))
    enrollment_method = db.Column(db.String(20), default='manual')  # 'manual', 'self_enrollment'
    status = db.Column(db.String(20), default='active')  # 'active', 'dropped', 'completed'
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    __table_args__ = (
        db.UniqueConstraint('student_id', 'class_instance_id', name='unique_enrollment'),
    )


class AttendanceRecord(db.Model):
    """Attendance record model"""
    __tablename__ = 'attendance_records'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    session_id = db.Column(db.String(36), db.ForeignKey('class_sessions.id'), nullable=False)
    student_id = db.Column(db.String(36), db.ForeignKey('students.user_id'), nullable=False)
    
    scanned_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default='present')  # 'present', 'late', 'absent', 'excused'
    minutes_late = db.Column(db.Integer, default=0)
    
    device_fingerprint = db.Column(db.String(255))
    ip_address = db.Column(db.String(50))
    qr_secret_used = db.Column(db.String(255))
    verification_method = db.Column(db.String(50), default='qr_standard')  # 'webauthn_early', 'webauthn_instant', 'qr_standard', 'manual_override'
    
    # Geolocation fields
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    location_verified = db.Column(db.Boolean, default=False)
    location_distance = db.Column(db.Float)  # Distance from classroom in meters
    
    status_changed_by = db.Column(db.String(36), db.ForeignKey('users.id'))
    status_changed_at = db.Column(db.DateTime)
    status_change_reason = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    audit_logs = db.relationship('AttendanceAuditLog', backref='attendance_record', lazy='dynamic', cascade='all, delete-orphan')
    
    __table_args__ = (
        db.UniqueConstraint('session_id', 'student_id', name='unique_attendance'),
    )


class Notification(db.Model):
    """Notification model"""
    __tablename__ = 'notifications'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    type = db.Column(db.String(50), nullable=False)  # 'attendance_recorded', 'session_started', etc.
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text)
    link = db.Column(db.String(500))
    session_id = db.Column(db.String(36))
    extra_data = db.Column(db.Text)  # JSON data (renamed from metadata which is reserved)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class WebAuthnCredential(db.Model):
    """Registered WebAuthn credential (fingerprint/FIDO2 authenticator)"""
    __tablename__ = 'webauthn_credentials'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    credential_id = db.Column(db.String(500), unique=True, nullable=False, index=True)
    public_key = db.Column(db.Text, nullable=False)
    sign_count = db.Column(db.Integer, default=0)
    transports = db.Column(db.String(255))
    nickname = db.Column(db.String(100), default='Biometric Authenticator')
    aaguid = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)


class EarlyBiometricVerification(db.Model):
    """5-minute early biometric verification record bound to a class session"""
    __tablename__ = 'early_biometric_verifications'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    student_id = db.Column(db.String(36), db.ForeignKey('students.user_id'), nullable=False, index=True)
    session_id = db.Column(db.String(36), db.ForeignKey('class_sessions.id'), nullable=False, index=True)
    credential_id = db.Column(db.String(500), nullable=False)
    verified_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default='valid')  # 'valid', 'consumed', 'expired'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        db.UniqueConstraint('student_id', 'session_id', name='unique_student_session_verification'),
    )


class AttendanceAuditLog(db.Model):
    """Audit trail for manual attendance adjustments"""
    __tablename__ = 'attendance_audit_logs'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    attendance_id = db.Column(db.String(36), db.ForeignKey('attendance_records.id'), nullable=False)
    session_id = db.Column(db.String(36), db.ForeignKey('class_sessions.id'), nullable=False)
    student_id = db.Column(db.String(36), db.ForeignKey('students.user_id'), nullable=False)
    changed_by_user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    previous_status = db.Column(db.String(20), nullable=False)
    new_status = db.Column(db.String(20), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    ip_address = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class WebAuthnChallenge(db.Model):
    """Ephemeral challenge storage for registration and authentication"""
    __tablename__ = 'webauthn_challenges'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    challenge = db.Column(db.String(255), nullable=False)
    session_id = db.Column(db.String(36))
    challenge_type = db.Column(db.String(50), nullable=False)  # 'registration', 'authentication', 'early_verification'
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

