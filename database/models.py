# database/models.py
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Boolean, JSON, ForeignKey, Index, Enum as SQLAlchemyEnum
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime, timezone

# Используем Enum из общего файла
from database.enums import SubscriptionTier, SubscriptionStatus

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    language_code = Column(String(10), default='ru')
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    last_activity = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
    
    country_code = Column(String(3), nullable=True, index=True) # Для гео-ограничений промокодов

    # Связи
    conversations = relationship("Conversation", back_populates="user", cascade="all, delete-orphan")
    preferences = relationship("UserPreference", back_populates="user", cascade="all, delete-orphan")
    insights = relationship("UserInsight", back_populates="user", cascade="all, delete-orphan")
    journal_entries = relationship("JournalEntry", back_populates="user", cascade="all, delete-orphan")
    memories = relationship("Memory", back_populates="user", cascade="all, delete-orphan")
    statistics_entries = relationship("BotStatistics", back_populates="user_obj", cascade="all, delete-orphan")
    error_logs = relationship("ErrorLog", back_populates="user_obj", cascade="all, delete-orphan")
    referral_code_entry = relationship("ReferralCode", back_populates="user", uselist=False, cascade="all, delete-orphan")
    action_timestamps = relationship("UserActionTimestamp", back_populates="user", cascade="all, delete-orphan")
    temporary_blocks = relationship("TemporaryBlock", back_populates="user", cascade="all, delete-orphan")
    context_summaries = relationship("ContextSummary", back_populates="user", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="user", cascade="all, delete-orphan", order_by="desc(Subscription.activated_at)")

class Subscription(Base):
    __tablename__ = 'subscriptions'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False, index=True)
    
    tier = Column(SQLAlchemyEnum(SubscriptionTier, name="subscription_tier_enum", create_type=False), 
                  default=SubscriptionTier.FREE, nullable=False, index=True)
    status = Column(SQLAlchemyEnum(SubscriptionStatus, name="subscription_status_enum", create_type=False), 
                    default=SubscriptionStatus.ACTIVE, nullable=False, index=True)
    
    activated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    expires_at = Column(DateTime, nullable=True, index=True) 
    
    is_trial = Column(Boolean, default=False, nullable=False)
    trial_source = Column(String(100), nullable=True) 
    
    payment_provider = Column(String(100), nullable=True) 
    telegram_charge_id = Column(String(255), nullable=True, unique=True, index=True) 
    payment_amount_stars = Column(Integer, nullable=True) # Сумма, фактически уплаченная
    
    auto_renewal = Column(Boolean, default=False, nullable=False) 
    original_tier_before_expiry = Column(SQLAlchemyEnum(SubscriptionTier, name="subscription_tier_enum_orig", create_type=False), nullable=True)
    
    # Поля для отслеживания промокода, примененного к этой подписке
    applied_promocode_id = Column(Integer, ForeignKey('promo_codes.id', name='fk_subscription_promocode_id', ondelete="SET NULL"), nullable=True, index=True)
    applied_promocode_code = Column(String(100), nullable=True, index=True) # Сохраняем сам код для удобства
    discount_applied_stars = Column(Integer, nullable=True, default=0) # Сумма скидки, если была

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("User", back_populates="subscriptions")
    applied_promocode = relationship("PromoCode", back_populates="subscription_applications") # Связь с PromoCode

    __table_args__ = (
        Index('idx_subscription_user_status_expires', 'user_id', 'status', 'expires_at'),
    )

class Conversation(Base):
    __tablename__ = 'conversations'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False, index=True)
    persona = Column(String(50), default='aeris', nullable=False, index=True)
    current_vibe = Column(String(50), default='friend', nullable=True)
    sexting_level = Column(Integer, default=0, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
    
    user = relationship("User", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at")

class Message(Base):
    __tablename__ = 'messages'
    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey('conversations.id', ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False, index=True) 
    content = Column(Text, nullable=False)
    message_type = Column(String(50), default='text', nullable=False) 
    tokens_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    message_metadata = Column(JSON, nullable=True) 
    
    conversation = relationship("Conversation", back_populates="messages")

class UserPreference(Base):
    __tablename__ = 'user_preferences'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False, index=True)
    persona = Column(String(50), default='aeris', nullable=False, index=True) 
    preference_key = Column(String(255), nullable=False, index=True)
    preference_value = Column(Text, nullable=True)
    preference_type = Column(String(50), default='string', nullable=False) 
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
    
    user = relationship("User", back_populates="preferences")
    __table_args__ = (Index('idx_user_persona_key_preference', 'user_id', 'persona', 'preference_key', unique=True),)

class UserInsight(Base):
    __tablename__ = 'user_insights'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    category = Column(String(100), nullable=True) 
    tags = Column(String(500), nullable=True) 
    relevance_score = Column(Float, default=1.0, nullable=False)
    access_count = Column(Integer, default=0, nullable=False)
    last_accessed = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    
    user = relationship("User", back_populates="insights")

class JournalEntry(Base):
    __tablename__ = 'journal_entries'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(500), nullable=True)
    content = Column(Text, nullable=False)
    mood = Column(String(100), nullable=True) 
    tags = Column(String(500), nullable=True) 
    reflection_count = Column(Integer, default=0, nullable=False) 
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
    
    user = relationship("User", back_populates="journal_entries")

class Memory(Base):
    __tablename__ = 'memories'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False, index=True)
    persona = Column(String(50), default='aeris', nullable=False, index=True) 
    content = Column(Text, nullable=False)
    memory_type = Column(String(50), nullable=False, index=True) 
    relevance_score = Column(Float, default=0.5, nullable=False) 
    emotional_weight = Column(Float, default=0.5, nullable=False) 
    tags = Column(String(500), nullable=True) 
    context = Column(Text, nullable=True) 
    access_count = Column(Integer, default=0, nullable=False) 
    last_accessed = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
    tier_created = Column(String(50), nullable=True, index=True) 
    expires_at = Column(DateTime, nullable=True, index=True) 
    priority = Column(Integer, default=3, nullable=False, index=True) 
    
    user = relationship("User", back_populates="memories")
    __table_args__ = (Index('idx_memory_user_persona_expires_priority', 'user_id', 'persona', 'expires_at', 'priority'),)

class ContextSummary(Base):
    __tablename__ = 'context_summaries'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False, index=True)
    persona = Column(String(50), default='aeris', nullable=False, index=True)
    summary_text = Column(Text, nullable=False)
    message_count = Column(Integer, nullable=False) 
    summary_period_start_at = Column(DateTime, nullable=False)
    summary_period_end_at = Column(DateTime, nullable=False, index=True)
    tokens_saved = Column(Integer, default=0, nullable=False) 
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    
    user = relationship("User", back_populates="context_summaries")
    __table_args__ = (Index('idx_summary_user_persona_end_at', 'user_id', 'persona', 'summary_period_end_at'),)

class FileUpload(Base):
    __tablename__ = 'file_uploads'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False, index=True)
    file_name = Column(String(500), nullable=False)
    file_type = Column(String(100), nullable=False) 
    file_size = Column(Integer, nullable=True) 
    file_path = Column(String(1000), nullable=True) 
    telegram_file_id = Column(String(500), nullable=True, unique=True) 
    processing_status = Column(String(50), default='pending', nullable=False) 
    content_preview = Column(Text, nullable=True) 
    file_metadata = Column(JSON, nullable=True) 
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

class BotStatistics(Base):
    __tablename__ = 'bot_statistics'
    id = Column(Integer, primary_key=True)
    date = Column(DateTime, default=lambda: datetime.now(timezone.utc).date(), nullable=False, index=True) 
    metric_name = Column(String(255), nullable=False, index=True) 
    metric_value = Column(Float, nullable=False) 
    user_id = Column(Integer, ForeignKey('users.id', ondelete="SET NULL"), nullable=True, index=True) 
    persona = Column(String(50), nullable=True, index=True) 
    additional_data = Column(JSON, nullable=True) 
    
    user_obj = relationship("User", back_populates="statistics_entries")
    __table_args__ = (Index('idx_stats_date_name', 'date', 'metric_name'),)

class ErrorLog(Base):
    __tablename__ = 'error_logs'
    id = Column(Integer, primary_key=True)
    error_id = Column(String(255), nullable=False, unique=True, index=True) 
    user_id = Column(Integer, ForeignKey('users.id', ondelete="SET NULL"), nullable=True, index=True) 
    error_type = Column(String(255), nullable=False, index=True)
    error_message = Column(Text, nullable=False)
    stack_trace = Column(Text, nullable=True)
    context_data = Column(JSON, nullable=True) 
    severity = Column(String(50), default='ERROR', nullable=False, index=True) 
    resolved = Column(Boolean, default=False, nullable=False, index=True) 
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    
    user_obj = relationship("User", back_populates="error_logs")

class ReferralCode(Base):
    __tablename__ = 'referral_codes'
    id = Column(Integer, primary_key=True, index=True)
    user_id_db = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), unique=True, nullable=False, index=True) 
    code = Column(String(50), unique=True, nullable=False, index=True) 
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    
    user = relationship("User", back_populates="referral_code_entry")
    __table_args__ = (Index('idx_referral_code_unique', 'code', unique=True),)

class UserActionTimestamp(Base):
    __tablename__ = 'user_action_timestamps'
    id = Column(Integer, primary_key=True, index=True)
    user_id_db = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False, index=True)
    action_key = Column(String(255), nullable=False, index=True) 
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True) 
    user = relationship("User", back_populates="action_timestamps")
    __table_args__ = (Index('idx_user_action_timestamp_key_time', 'user_id_db', 'action_key', 'timestamp'),)

class PromoCode(Base):
    __tablename__ = 'promo_codes'
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(100), unique=True, nullable=False, index=True)
    discount_type = Column(String(50), nullable=False)  
    discount_value = Column(Float, nullable=False)
    max_uses = Column(Integer, nullable=True) 
    uses_count = Column(Integer, default=0, nullable=False)
    max_uses_per_user = Column(Integer, nullable=True) 
    user_specific_id = Column(Integer, ForeignKey('users.id', ondelete="SET NULL"), nullable=True, index=True) 
    active_from = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
    description = Column(Text, nullable=True) 
    user_facing_description = Column(Text, nullable=True) 
    code_type = Column(String(50), default='generic', nullable=False) 
    trial_tier_target = Column(String(50), nullable=True) 
    for_subscription_tier = Column(String(50), nullable=True) 
    min_purchase_amount = Column(Integer, nullable=True) 
    created_by_admin_id = Column(Integer, nullable=True) 
    is_for_first_time_users = Column(Boolean, default=False, nullable=False)
    is_for_upgrade_only = Column(Boolean, default=False, nullable=False)
    is_seasonal = Column(Boolean, default=False, nullable=False)
    seasonal_event = Column(String(255), nullable=True)
    min_account_age_days = Column(Integer, nullable=True)
    allowed_user_segments = Column(JSON, nullable=True) 
    allowed_countries = Column(JSON, nullable=True)
    blocked_countries = Column(JSON, nullable=True)
    bonus_message_expiry_days = Column(Integer, nullable=True)
    
    # Связь с подписками, к которым он был применен
    subscription_applications = relationship("Subscription", back_populates="applied_promocode")


class TemporaryBlock(Base):
    __tablename__ = 'temporary_blocks'
    id = Column(Integer, primary_key=True, index=True)
    user_id_db = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False, index=True)
    block_type = Column(String(100), nullable=False, default="spam_activity") 
    blocked_until_utc = Column(DateTime, nullable=False, index=True) 
    reason = Column(Text, nullable=True) 
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    user = relationship("User", back_populates="temporary_blocks")
    __table_args__ = (Index('idx_temp_block_user_until', 'user_id_db', 'blocked_until_utc'),)
