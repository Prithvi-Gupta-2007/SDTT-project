"""
Seed initial data for the database
"""
from datetime import datetime, date


def seed_initial_data():
    """Seed the database with initial data"""
    from app import db
    from app.models import (
        Department, AcademicPeriod, Course, User, Professor, 
        Student, ClassInstance, ClassSession, Enrollment
    )
    
    # Check if data already exists
    if Department.query.first() and User.query.filter_by(email='professor@acem.ac.in').first():
        return
    
    print("[INFO] Seeding initial data...")
    
    # Create departments
    departments = [
        Department(id='dept-1', code='CSC', name='Computer Science'),
        Department(id='dept-2', code='MAT', name='Mathematics'),
        Department(id='dept-3', code='PHY', name='Physics'),
        Department(id='dept-4', code='ENG', name='English'),
        Department(id='dept-5', code='ECE', name='Electronics & Communication'),
    ]
    
    for dept in departments:
        db.session.add(dept)
    
    # Create academic periods
    current_year = datetime.now().year
    periods = [
        AcademicPeriod(
            id='period-1',
            name=f'Spring {current_year}',
            year=current_year,
            semester='spring',
            start_date=date(current_year, 1, 15),
            end_date=date(current_year, 5, 15),
            is_current=True,
            is_active=True
        ),
        AcademicPeriod(
            id='period-2',
            name=f'Fall {current_year}',
            year=current_year,
            semester='fall',
            start_date=date(current_year, 8, 15),
            end_date=date(current_year, 12, 15),
            is_current=False,
            is_active=True
        ),
    ]
    
    for period in periods:
        db.session.add(period)
    
    # Create courses
    courses = [
        Course(id='course-1', code='CSC-101', name='Introduction to Computer Science', description='Fundamentals of computing and programming', credits=3, department_id='dept-1'),
        Course(id='course-2', code='CSC-201', name='Data Structures', description='Study of data organization and algorithms', credits=4, department_id='dept-1'),
        Course(id='course-3', code='CSC-301', name='Database Systems', description='Design and implementation of database systems', credits=3, department_id='dept-1'),
        Course(id='course-4', code='MAT-101', name='Calculus I', description='Differential and integral calculus', credits=4, department_id='dept-2'),
        Course(id='course-5', code='MAT-201', name='Linear Algebra', description='Vector spaces and linear transformations', credits=3, department_id='dept-2'),
        Course(id='course-6', code='PHY-101', name='Physics I', description='Mechanics and thermodynamics', credits=4, department_id='dept-3'),
        Course(id='course-7', code='ECE-101', name='Basic Electronics', description='Introduction to electronic circuits', credits=3, department_id='dept-5'),
    ]
    
    for course in courses:
        db.session.add(course)
    
    db.session.commit()

    # Seed default test professor and student if needed
    from app.models import User, Student, Professor, ClassInstance, ClassSession, Enrollment
    from werkzeug.security import generate_password_hash
    import json
    import random
    import string
    from datetime import timedelta

    if not User.query.filter_by(email='professor@acem.ac.in').first():
        prof_user = User(
            id='user-prof-001',
            email='professor@acem.ac.in',
            password_hash=generate_password_hash('password123'),
            first_name='John',
            last_name='Smith',
            role='professor',
            is_active=True
        )
        db.session.add(prof_user)
        db.session.flush()

        dept = Department.query.first()
        professor = Professor(
            id='prof-profile-001',
            user_id='user-prof-001',
            employee_id='EMP001',
            department_id=dept.id if dept else None,
            title='Prof.'
        )
        db.session.add(professor)

        student_user = User(
            id='user-stu-001',
            email='student@acem.ac.in',
            password_hash=generate_password_hash('password123'),
            first_name='Jane',
            last_name='Doe',
            role='student',
            is_active=True
        )
        db.session.add(student_user)
        db.session.flush()

        student = Student(
            id='stu-profile-001',
            user_id='user-stu-001',
            student_id='STU001',
            major='Computer Science',
            enrollment_year=2024
        )
        db.session.add(student)
        db.session.commit()

        # Seed sample class
        period = AcademicPeriod.query.filter_by(is_current=True).first()
        course = Course.query.first()
        if period and course:
            today = date.today()
            class_code = f"{course.code}-{''.join(random.choices(string.ascii_uppercase, k=6))}"
            class_instance = ClassInstance(
                course_id=course.id,
                professor_id=prof_user.id,
                academic_period_id=period.id,
                section_number=1,
                class_code=class_code,
                days_of_week=json.dumps(['Monday', 'Wednesday', 'Friday']),
                start_time='09:00',
                end_time='10:00',
                first_class_date=today - timedelta(days=30),
                last_class_date=today + timedelta(days=60),
                room_location='Room 101',
                max_students=50,
                is_active=True
            )
            db.session.add(class_instance)
            db.session.flush()

            enrollment = Enrollment(
                student_id=student_user.id,
                class_instance_id=class_instance.id,
                status='active'
            )
            db.session.add(enrollment)

            for i in range(5):
                session_date = today - timedelta(days=i*2)
                session = ClassSession(
                    class_instance_id=class_instance.id,
                    session_number=5-i,
                    date=session_date,
                    start_time='09:00',
                    end_time='10:00',
                    status='completed' if i > 0 else 'scheduled'
                )
                db.session.add(session)
            db.session.commit()

    print("[OK] Initial data seeded successfully!")
