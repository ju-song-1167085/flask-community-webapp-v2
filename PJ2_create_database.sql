-- =============================================
-- ActiveLoop Plus - Final Optimized Schema v4 (updated)
-- 10 tables + 2 views
-- =============================================
DROP DATABASE IF EXISTS eventbridge_plus;
CREATE DATABASE eventbridge_plus CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE eventbridge_plus;

-- =============================================
-- 1) Users - System user management
-- =============================================
CREATE TABLE users (
    user_id INT PRIMARY KEY AUTO_INCREMENT,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    first_name VARCHAR(50) NOT NULL,
    last_name VARCHAR(50) NOT NULL,
    location VARCHAR(50) NULL,
    gender ENUM('male', 'female', 'other', 'prefer_not_to_say') NULL,
    birth_date DATE NULL,
    biography TEXT NULL,
    user_image VARCHAR(255) NULL,
    platform_role ENUM('participant', 'super_admin', 'support_technician') NOT NULL DEFAULT 'participant',
    status ENUM('active', 'banned') NOT NULL DEFAULT 'active',
    notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    
    -- Account ban related (internal only)
    banned_reason VARCHAR(255) NULL,
    banned_by INT NULL,
    banned_at TIMESTAMP NULL,
    
    FOREIGN KEY (banned_by) REFERENCES users(user_id) ON DELETE SET NULL,
    INDEX idx_platform_role (platform_role),
    INDEX idx_status (status),
    INDEX idx_location (location),
    INDEX idx_email (email),
    INDEX idx_username (username),
    INDEX idx_notifications (notifications_enabled)
);

-- =============================================
-- 2) Group Info - Group information and application management
-- =============================================
CREATE TABLE group_info (
    group_id INT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(100) NOT NULL,
    description TEXT NOT NULL,
    group_type ENUM('activity','social','mixed') NOT NULL DEFAULT 'mixed',
    group_location  VARCHAR(100) NULL,
    is_public BOOLEAN NOT NULL DEFAULT TRUE,
    max_members INT NOT NULL DEFAULT 500,
    status ENUM('draft', 'pending', 'approved', 'rejected', 'inactive') NOT NULL DEFAULT 'draft',
    -- Internal reference rejection reason (optional)
    rejection_reason ENUM('inappropriate_content', 'duplicate_group', 'insufficient_info', 'guideline_violation', 'other') NULL,
    -- Initial members proposed during group creation (comma-separated user_ids)
    first_members VARCHAR(255) NULL COMMENT 'Comma-separated usernames of proposed initial members',
    created_by INT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    FOREIGN KEY (created_by) REFERENCES users(user_id) ON DELETE CASCADE,
    INDEX idx_status (status),
    INDEX idx_created_by (created_by),
    INDEX idx_group_type (group_type),
    INDEX idx_status_created (status, created_at),
    INDEX idx_active_groups (status, is_public),
    INDEX idx_group_location (group_location),  
    CONSTRAINT check_max_members CHECK (max_members > 0)
);

-- =============================================
-- 3) Group Memberships - Group membership management
-- =============================================
CREATE TABLE group_members (
    membership_id INT PRIMARY KEY AUTO_INCREMENT,
    user_id INT NOT NULL,
    group_id INT NOT NULL,
    group_role ENUM('member', 'volunteer', 'manager') NOT NULL DEFAULT 'member',
    status ENUM('active', 'pending', 'rejected', 'left') NOT NULL DEFAULT 'active',
    join_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    left_date TIMESTAMP NULL,
    
    -- Internal reference removal reason (optional)
    removal_reason ENUM('inactive', 'rule_violation', 'inappropriate_behavior', 'other') NULL,
    
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (group_id) REFERENCES group_info(group_id) ON DELETE CASCADE,
    UNIQUE KEY uniq_user_group (user_id, group_id),
    INDEX idx_user_id (user_id),
    INDEX idx_group_id (group_id),
    INDEX idx_status (status),
    INDEX idx_group_role (group_role),
    INDEX idx_user_group_role (user_id, group_role, status),
    CONSTRAINT check_left_after_join CHECK (left_date IS NULL OR left_date >= join_date)
);

-- =============================================
-- 4) Group Join Requests - Group join request management
-- =============================================
CREATE TABLE group_requests (
    request_id INT PRIMARY KEY AUTO_INCREMENT,
    group_id INT NOT NULL,
    user_id INT NOT NULL,
    message VARCHAR(255) NULL,
    status ENUM('pending', 'approved', 'rejected') NOT NULL DEFAULT 'pending',
    requested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- Internal reference rejection reason (optional)
    rejection_reason ENUM('group_full', 'activity_mismatch', 'insufficient_info', 'other') NULL,
    
    FOREIGN KEY (group_id) REFERENCES group_info(group_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    UNIQUE KEY uniq_user_group_request (user_id, group_id),
    INDEX idx_group_id (group_id),
    INDEX idx_user_id (user_id),
    INDEX idx_status (status)
);

-- =============================================
-- 5) Event Info - Event management 
-- =============================================
CREATE TABLE event_info (
    event_id INT PRIMARY KEY AUTO_INCREMENT,
    group_id INT NOT NULL,
    event_title VARCHAR(200) NOT NULL,
    description TEXT NULL,
    event_type ENUM('Swimming', 'Trail Running', 'Cycling', 'Park Walk', 'Fun Run', 'Marathon') NOT NULL,
    event_date DATE NOT NULL,
    event_time TIME NOT NULL,
    location VARCHAR(100) NOT NULL,
    max_participants INT NOT NULL,
    status ENUM('draft', 'scheduled', 'ongoing', 'completed', 'cancelled') NOT NULL DEFAULT 'draft',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    FOREIGN KEY (group_id) REFERENCES group_info(group_id) ON DELETE CASCADE,
    INDEX idx_group_id (group_id),
    INDEX idx_event_date (event_date),
    INDEX idx_status (status),
    INDEX idx_event_type (event_type),
    INDEX idx_date_status (event_date, status),
    -- helpful composite
    INDEX idx_group_eventdate (group_id, event_date),
    CONSTRAINT check_max_participants CHECK (max_participants > 0)
);

-- =============================================
-- 6) Event Memberships - Event participant and volunteer integrated management
-- =============================================
CREATE TABLE event_members (
    membership_id INT PRIMARY KEY AUTO_INCREMENT,
    event_id INT NOT NULL,
    user_id INT NOT NULL,
    event_role ENUM('participant', 'volunteer') NOT NULL DEFAULT 'participant',
    
    -- Common fields
    participation_status ENUM('registered', 'attended', 'cancelled', 'no_show') NOT NULL DEFAULT 'registered',
    registration_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- Volunteer-only fields (NULL when participant)
    responsibility ENUM('event_setup', 'safety_medical', 'participant_support', 'community_outreach', 'photography') NULL,
    volunteer_status ENUM('assigned', 'confirmed', 'cancelled', 'completed') NULL,
    volunteer_interests ENUM('event_setup', 'safety_medical', 'participant_support', 'community_outreach', 'photography') NULL,
    volunteer_hours DECIMAL(5,2) NULL DEFAULT NULL,
    assigned_at TIMESTAMP NULL,
    
    FOREIGN KEY (event_id) REFERENCES event_info(event_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    UNIQUE KEY uniq_user_event (user_id, event_id),
    INDEX idx_event_id (event_id),
    INDEX idx_user_id (user_id),
    INDEX idx_event_role (event_role),
    INDEX idx_participation_status (participation_status),
    INDEX idx_volunteer_status (volunteer_status),
    CONSTRAINT check_volunteer_hours_positive CHECK (volunteer_hours IS NULL OR volunteer_hours >= 0),
    CONSTRAINT check_volunteer_fields CHECK (
        (event_role = 'participant'
            AND volunteer_status IS NULL
            AND responsibility IS NULL
            AND volunteer_hours IS NULL)
        OR
        (event_role = 'volunteer' AND (volunteer_hours IS NULL OR volunteer_hours >= 0))
    )
);

-- =============================================
-- 7) Race Results - Race result management (Time Results Epic)
-- =============================================
CREATE TABLE race_results (
    membership_id INT PRIMARY KEY,
    start_time DATETIME NULL,
    finish_time DATETIME NULL,
    race_rank INT NULL,
    method ENUM('manual', 'csv_import') NOT NULL DEFAULT 'manual',
    status ENUM('draft', 'published') NOT NULL DEFAULT 'draft',
    recorded_by INT NULL,
    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (membership_id) REFERENCES event_members(membership_id) ON DELETE CASCADE,
    FOREIGN KEY (recorded_by) REFERENCES users(user_id) ON DELETE SET NULL,
    INDEX idx_status (status),
    INDEX idx_race_rank (race_rank),
    INDEX idx_recorded_by (recorded_by),
    CONSTRAINT check_rank_positive CHECK (race_rank IS NULL OR race_rank > 0),
    CONSTRAINT check_finish_after_start CHECK (start_time IS NULL OR finish_time IS NULL OR finish_time > start_time)
);

-- =============================================
-- 8) Help Requests - Helpdesk request management (Helpdesk Epic)
-- =============================================
CREATE TABLE help_requests (
    request_id INT PRIMARY KEY AUTO_INCREMENT,
    user_id INT NOT NULL,
    category ENUM('technical_issue','account_problem','event_inquiry','group_management','rejection_inquiry','general_help') NOT NULL DEFAULT 'general_help',
    title VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    priority ENUM('low', 'medium', 'high', 'urgent') NOT NULL DEFAULT 'medium',
    status ENUM('new', 'assigned', 'blocked', 'solved') NOT NULL DEFAULT 'new',
    assigned_to INT NULL,
    escalation_level ENUM('none', 'to_super_admin') NOT NULL DEFAULT 'none',
    escalated_at TIMESTAMP NULL,
    last_staff_reply_at TIMESTAMP NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP NULL,
    
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (assigned_to) REFERENCES users(user_id) ON DELETE SET NULL,
    INDEX idx_user_id (user_id),
    INDEX idx_assigned_to (assigned_to),
    INDEX idx_status (status),
    INDEX idx_priority (priority),
    INDEX idx_created_at (created_at),
    INDEX idx_category (category),
    INDEX idx_status_priority_created (status, priority, created_at)
);

-- =============================================
-- 9) Help Replies - Helpdesk reply management (thread-based)
-- =============================================
CREATE TABLE help_replies (
    reply_id INT PRIMARY KEY AUTO_INCREMENT,
    request_id INT NOT NULL,
    sender_id INT NOT NULL,
    reply_content TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (request_id) REFERENCES help_requests(request_id) ON DELETE CASCADE,
    FOREIGN KEY (sender_id) REFERENCES users(user_id) ON DELETE CASCADE,
    INDEX idx_request_id (request_id),
    INDEX idx_sender_id (sender_id),
    INDEX idx_created_at (created_at)
);

-- =============================================
-- 10) Notifications - Notification management
-- =============================================
CREATE TABLE notifications (
    notification_id INT PRIMARY KEY AUTO_INCREMENT,
    user_id INT NOT NULL,
    title VARCHAR(200) NOT NULL,
    message TEXT NOT NULL,
    related_id INT NULL,
    category ENUM('event', 'group', 'volunteer', 'system') NOT NULL,
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    INDEX idx_user_id (user_id),
    INDEX idx_is_read (is_read),
    INDEX idx_category (category),
    INDEX idx_created_at (created_at),
    INDEX idx_user_unread (user_id, is_read)
);

-- =============================================
-- Analytics Views (Analytics Epic)
-- =============================================

-- User activity summary view
CREATE OR REPLACE VIEW user_activity_summary AS
SELECT 
    u.user_id,
    u.username,
    u.first_name,
    u.last_name,
    u.platform_role,
    u.location,
    COUNT(DISTINCT CASE WHEN gm.status = 'active' THEN gm.group_id END) AS groups_joined,
    COUNT(DISTINCT em.event_id) AS events_participated,
    COUNT(DISTINCT CASE WHEN em.event_role = 'volunteer' THEN em.event_id END) AS volunteer_events,
    COALESCE(SUM(CASE WHEN em.event_role = 'volunteer' THEN em.volunteer_hours END), 0) AS total_volunteer_hours,
    COUNT(DISTINCT CASE WHEN em.participation_status = 'attended' THEN em.event_id END) AS events_attended,
    COUNT(DISTINCT CASE WHEN ug.status = 'approved' AND ug.created_by = u.user_id THEN ug.group_id END) AS groups_created
FROM users u
LEFT JOIN group_members gm ON u.user_id = gm.user_id
LEFT JOIN event_members em ON u.user_id = em.user_id
LEFT JOIN group_info ug ON u.user_id = ug.created_by
WHERE u.status = 'active'
GROUP BY u.user_id, u.username, u.first_name, u.last_name, u.platform_role, u.location;

-- Group activity summary view
CREATE OR REPLACE VIEW group_activity_summary AS
SELECT 
    g.group_id,
    g.name AS group_name,
    g.group_type,
    g.status AS group_status,
    g.is_public,
    g.max_members,
    g.created_by,
    CONCAT(u.first_name, ' ', u.last_name) AS creator_name,
    g.created_at,
    g.updated_at,
    COUNT(DISTINCT CASE WHEN gm.status = 'active' THEN gm.user_id END) AS current_member_count,
    COUNT(DISTINCT e.event_id) AS total_events,
    COUNT(DISTINCT CASE WHEN e.status = 'completed' THEN e.event_id END) AS completed_events,
    COUNT(DISTINCT CASE WHEN e.status = 'scheduled' AND e.event_date >= CURDATE() THEN e.event_id END) AS upcoming_events,
    COUNT(DISTINCT em.user_id) AS unique_participants,
    COALESCE(AVG(CASE WHEN e.status NOT IN ('cancelled', 'draft') THEN 
        (SELECT COUNT(*)
        FROM event_members em2 
        WHERE em2.event_id = e.event_id 
        AND em2.participation_status IN ('registered', 'attended'))
    END), 0) AS avg_event_attendance
FROM group_info g
LEFT JOIN users u ON g.created_by = u.user_id
LEFT JOIN group_members gm ON g.group_id = gm.group_id
LEFT JOIN event_info e ON g.group_id = e.group_id
LEFT JOIN event_members em ON e.event_id = em.event_id AND em.participation_status = 'attended'
GROUP BY g.group_id, g.name, g.group_type, g.status, g.is_public, g.max_members, 
        g.created_by, u.first_name, u.last_name, g.created_at, g.updated_at;