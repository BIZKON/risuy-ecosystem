--
-- PostgreSQL database dump
--

\restrict gcIqSccRRU0DgziJf95Z9Q9mi0HlGXbmegl2HmZJbnQmfplF6R2y0BUZGxL7Ud2

-- Dumped from database version 16.14 (Ubuntu 16.14-1.pgdg24.04+1)
-- Dumped by pg_dump version 16.11

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: order_exists_for_payment(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.order_exists_for_payment(p_payment_id text) RETURNS boolean
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
    select exists (select 1 from orders where provider_payment_id = p_payment_id)
$$;


--
-- Name: order_tenant_by_id(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.order_tenant_by_id(p_order_id uuid) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
    select tenant_id from orders where id = p_order_id
$$;


--
-- Name: order_tenant_for_payment(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.order_tenant_for_payment(p_payment_id text) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
    select tenant_id from orders where provider_payment_id = p_payment_id
$$;


--
-- Name: set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
begin
    new.updated_at = now();
    return new;
end;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: account_identities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_identities (
    id bigint NOT NULL,
    provider text NOT NULL,
    external_id text NOT NULL,
    username text NOT NULL,
    verified boolean DEFAULT false NOT NULL,
    display_name text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_login_at timestamp with time zone,
    CONSTRAINT account_identities_provider_check CHECK ((provider = ANY (ARRAY['email'::text, 'phone'::text, 'vk'::text, 'telegram'::text])))
);


--
-- Name: account_identities_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.account_identities ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.account_identities_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: admin_audit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_audit (
    id bigint NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    actor text NOT NULL,
    action text NOT NULL,
    lead_id uuid,
    ip inet,
    user_agent text,
    detail jsonb
);


--
-- Name: admin_audit_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.admin_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: admin_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.admin_audit_id_seq OWNED BY public.admin_audit.id;


--
-- Name: admin_login_throttle; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_login_throttle (
    account text NOT NULL,
    fail_count integer DEFAULT 0 NOT NULL,
    locked_until timestamp with time zone
);


--
-- Name: admin_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_sessions (
    sid uuid DEFAULT gen_random_uuid() NOT NULL,
    actor text NOT NULL,
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked boolean DEFAULT false NOT NULL,
    ip inet,
    ua text,
    search_phone_hash text,
    role text DEFAULT 'operator'::text NOT NULL,
    active_tenant_id uuid
);


--
-- Name: admin_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_users (
    username text NOT NULL,
    password_hash text NOT NULL,
    role text DEFAULT 'operator'::text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT admin_users_role_chk CHECK ((role = ANY (ARRAY['admin'::text, 'operator'::text])))
);


--
-- Name: agent_memory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_memory (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    agent_id uuid NOT NULL,
    kind text DEFAULT 'summary'::text NOT NULL,
    content text NOT NULL,
    embedding public.vector(768),
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_token_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_token_snapshots (
    agent_id bigint NOT NULL,
    tenant_id uuid NOT NULL,
    used_tokens bigint NOT NULL,
    taken_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: app_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_settings (
    key text NOT NULL,
    value text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: broadcast_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.broadcast_files (
    id bigint NOT NULL,
    broadcast_id bigint NOT NULL,
    filename text,
    mime text,
    bytes bytea,
    tg_file_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid NOT NULL
);


--
-- Name: broadcast_files_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.broadcast_files_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: broadcast_files_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.broadcast_files_id_seq OWNED BY public.broadcast_files.id;


--
-- Name: broadcast_recipients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.broadcast_recipients (
    id bigint NOT NULL,
    broadcast_id bigint NOT NULL,
    lead_id uuid NOT NULL,
    tg_user_id bigint,
    status text DEFAULT 'pending'::text NOT NULL,
    click_token text,
    attempts integer DEFAULT 0 NOT NULL,
    claimed_at timestamp with time zone,
    error text,
    sent_at timestamp with time zone,
    tenant_id uuid NOT NULL,
    messenger text DEFAULT 'tg'::text NOT NULL,
    reply_address bigint,
    CONSTRAINT br_status_chk CHECK ((status = ANY (ARRAY['pending'::text, 'sending'::text, 'sent'::text, 'failed'::text, 'skipped'::text])))
);


--
-- Name: broadcast_recipients_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.broadcast_recipients_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: broadcast_recipients_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.broadcast_recipients_id_seq OWNED BY public.broadcast_recipients.id;


--
-- Name: broadcasts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.broadcasts (
    id bigint NOT NULL,
    title text,
    messenger text DEFAULT 'tg'::text NOT NULL,
    kind text DEFAULT 'text'::text NOT NULL,
    body_template text NOT NULL,
    audience_filter jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    recipient_count integer,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    totals jsonb DEFAULT '{}'::jsonb NOT NULL,
    product_id bigint,
    tenant_id uuid NOT NULL,
    CONSTRAINT broadcasts_status_chk CHECK ((status = ANY (ARRAY['draft'::text, 'queued'::text, 'sending'::text, 'paused'::text, 'done'::text, 'canceled'::text])))
);


--
-- Name: broadcasts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.broadcasts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: broadcasts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.broadcasts_id_seq OWNED BY public.broadcasts.id;


--
-- Name: club_intros; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.club_intros (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    from_member uuid NOT NULL,
    to_member uuid NOT NULL,
    to_tenant_id uuid,
    status text DEFAULT 'requested'::text NOT NULL,
    message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    from_accepted_at timestamp with time zone,
    to_accepted_at timestamp with time zone,
    CONSTRAINT club_intros_status_check CHECK ((status = ANY (ARRAY['requested'::text, 'accepted'::text, 'declined'::text, 'cancelled'::text])))
);


--
-- Name: club_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.club_members (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    lead_id uuid,
    inn text,
    display_name text NOT NULL,
    city text,
    okved text,
    status text DEFAULT 'active'::text NOT NULL,
    network_opt_in boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tg_user_id bigint,
    vk_user_id bigint,
    max_user_id bigint,
    erase_requested_at timestamp with time zone,
    offers_opt_in boolean DEFAULT true NOT NULL,
    CONSTRAINT club_members_status_check CHECK ((status = ANY (ARRAY['active'::text, 'paused'::text, 'left'::text])))
);


--
-- Name: club_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.club_profiles (
    member_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    offering text,
    avg_check integer,
    seeking text,
    chain_position text,
    okved_seek text,
    description text,
    CONSTRAINT club_profiles_chain_position_check CHECK ((chain_position = ANY (ARRAY['before'::text, 'after'::text, 'both'::text])))
);


--
-- Name: consent_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.consent_events (
    id bigint NOT NULL,
    tenant_id uuid NOT NULL,
    lead_id uuid,
    doc_type text DEFAULT 'consent'::text NOT NULL,
    doc_version integer DEFAULT 1 NOT NULL,
    text_hash text,
    action text NOT NULL,
    channel text,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    ip inet,
    user_agent text,
    member_id uuid,
    CONSTRAINT consent_events_action_check CHECK ((action = ANY (ARRAY['granted'::text, 'revoked'::text])))
);


--
-- Name: consent_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.consent_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: consent_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.consent_events_id_seq OWNED BY public.consent_events.id;


--
-- Name: credit_wallets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.credit_wallets (
    tenant_id uuid NOT NULL,
    balance_microrub bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: kb_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kb_chunks (
    id bigint NOT NULL,
    document_id uuid NOT NULL,
    chunk_index integer NOT NULL,
    content text NOT NULL,
    embedding public.vector(768),
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid NOT NULL
);


--
-- Name: kb_chunks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.kb_chunks ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.kb_chunks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: kb_documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kb_documents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    title text NOT NULL,
    source text,
    role_tag text,
    content text NOT NULL,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid NOT NULL
);


--
-- Name: leads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leads (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    messenger text DEFAULT 'tg'::text NOT NULL,
    source text DEFAULT 'other'::text NOT NULL,
    name text,
    phone text,
    phone_hash text,
    consent boolean DEFAULT false NOT NULL,
    subscribed boolean DEFAULT false NOT NULL,
    status text DEFAULT 'new'::text NOT NULL,
    guide_sent_at timestamp with time zone,
    follow_up_1_at timestamp with time zone,
    follow_up_2_at timestamp with time zone,
    follow_up_3_at timestamp with time zone,
    tg_user_id bigint,
    max_user_id bigint,
    notes text,
    survey jsonb,
    erase_requested_at timestamp with time zone,
    bot_paused boolean DEFAULT false NOT NULL,
    unsubscribed_at timestamp with time zone,
    ai_persona text,
    tenant_id uuid NOT NULL,
    escalated_at timestamp with time zone,
    vk_user_id bigint,
    max_chat_id bigint,
    web_session_id text
);


--
-- Name: link_clicks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.link_clicks (
    id bigint NOT NULL,
    token text NOT NULL,
    broadcast_id bigint,
    lead_id uuid,
    clicked_at timestamp with time zone DEFAULT now() NOT NULL,
    ua text,
    ip inet,
    tenant_id uuid NOT NULL
);


--
-- Name: link_clicks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.link_clicks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: link_clicks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.link_clicks_id_seq OWNED BY public.link_clicks.id;


--
-- Name: link_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.link_tokens (
    token text NOT NULL,
    target_url text NOT NULL,
    broadcast_id bigint,
    lead_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid NOT NULL
);


--
-- Name: memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memberships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    username text NOT NULL,
    role text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memberships_role_check CHECK ((role = ANY (ARRAY['owner'::text, 'admin'::text, 'operator'::text])))
);


--
-- Name: messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.messages (
    id bigint NOT NULL,
    lead_id uuid,
    tg_user_id bigint,
    tg_message_id bigint,
    direction text NOT NULL,
    kind text DEFAULT 'text'::text NOT NULL,
    text text,
    file_id text,
    source text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid NOT NULL,
    messenger text DEFAULT 'tg'::text NOT NULL,
    CONSTRAINT messages_direction_chk CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text]))),
    CONSTRAINT messages_kind_chk CHECK ((kind = ANY (ARRAY['text'::text, 'photo'::text, 'document'::text, 'video'::text, 'voice'::text, 'video_note'::text, 'audio'::text, 'animation'::text, 'sticker'::text, 'other'::text])))
);


--
-- Name: messages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.messages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.messages_id_seq OWNED BY public.messages.id;


--
-- Name: model_prices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.model_prices (
    id bigint NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    price_in_microrub_per_1k bigint NOT NULL,
    price_out_microrub_per_1k bigint NOT NULL,
    effective_from timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: model_prices_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.model_prices ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.model_prices_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.orders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    lead_id uuid,
    product_id bigint,
    amount numeric(12,2) NOT NULL,
    currency text DEFAULT 'RUB'::text NOT NULL,
    status text DEFAULT 'paid'::text NOT NULL,
    source text DEFAULT 'manual'::text NOT NULL,
    provider_payment_id text,
    note text,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    paid_at timestamp with time zone,
    payment_url text,
    tenant_id uuid NOT NULL,
    CONSTRAINT orders_amount_chk CHECK ((amount >= (0)::numeric)),
    CONSTRAINT orders_currency_chk CHECK ((currency = ANY (ARRAY['RUB'::text, 'USD'::text, 'EUR'::text]))),
    CONSTRAINT orders_status_chk CHECK ((status = ANY (ARRAY['pending'::text, 'paid'::text, 'failed'::text, 'refunded'::text])))
);


--
-- Name: outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outbox (
    id bigint NOT NULL,
    lead_id uuid NOT NULL,
    tg_user_id bigint,
    kind text DEFAULT 'text'::text NOT NULL,
    text text,
    file_id text,
    status text DEFAULT 'queued'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    claimed_at timestamp with time zone,
    last_error text,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sent_at timestamp with time zone,
    file_bytes bytea,
    file_name text,
    file_mime text,
    upload_attempts integer DEFAULT 0 NOT NULL,
    upload_error text,
    tenant_id uuid NOT NULL,
    messenger text DEFAULT 'tg'::text NOT NULL,
    CONSTRAINT outbox_status_chk CHECK ((status = ANY (ARRAY['queued'::text, 'sending'::text, 'sent'::text, 'failed'::text])))
);


--
-- Name: outbox_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.outbox_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: outbox_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.outbox_id_seq OWNED BY public.outbox.id;


--
-- Name: partners; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.partners (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    ref_code text NOT NULL,
    tg_chat_id text,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT partners_status_chk CHECK ((status = ANY (ARRAY['active'::text, 'disabled'::text])))
);


--
-- Name: password_reset_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.password_reset_tokens (
    token_hash text NOT NULL,
    username text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    request_ip text
);


--
-- Name: payments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    type text NOT NULL,
    yookassa_payment_id text,
    idempotence_key text NOT NULL,
    amount_microrub bigint NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    captured_at timestamp with time zone,
    raw jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT payments_amount_microrub_check CHECK ((amount_microrub > 0)),
    CONSTRAINT payments_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'waiting_for_capture'::text, 'succeeded'::text, 'canceled'::text]))),
    CONSTRAINT payments_type_check CHECK ((type = ANY (ARRAY['subscription'::text, 'topup'::text])))
);


--
-- Name: plans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    price_microrub bigint NOT NULL,
    "interval" text DEFAULT 'month'::text NOT NULL,
    included_credits_microrub bigint DEFAULT 0 NOT NULL,
    billing_mode text DEFAULT 'cost_multiplier'::text NOT NULL,
    markup_multiplier numeric(4,2) DEFAULT 3.00 NOT NULL,
    per_message_microrub bigint,
    features jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT plans_billing_mode_check CHECK ((billing_mode = ANY (ARRAY['cost_multiplier'::text, 'per_message'::text]))),
    CONSTRAINT plans_interval_check CHECK (("interval" = ANY (ARRAY['month'::text, 'year'::text]))),
    CONSTRAINT plans_per_message_chk CHECK (((billing_mode <> 'per_message'::text) OR (per_message_microrub IS NOT NULL)))
);


--
-- Name: platform_notify; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_notify (
    id bigint NOT NULL,
    chat_id bigint NOT NULL,
    text text NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sent_at timestamp with time zone,
    claimed_at timestamp with time zone,
    CONSTRAINT platform_notify_status_chk CHECK ((status = ANY (ARRAY['queued'::text, 'sending'::text, 'sent'::text, 'failed'::text])))
);


--
-- Name: platform_notify_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.platform_notify_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: platform_notify_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.platform_notify_id_seq OWNED BY public.platform_notify.id;


--
-- Name: products; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.products (
    id bigint NOT NULL,
    name text NOT NULL,
    kind text NOT NULL,
    price numeric(12,2),
    currency text DEFAULT 'RUB'::text NOT NULL,
    caption text,
    link text,
    file bytea,
    file_name text,
    file_mime text,
    file_tg_id text,
    upload_attempts integer DEFAULT 0 NOT NULL,
    upload_error text,
    status text DEFAULT 'active'::text NOT NULL,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid NOT NULL,
    CONSTRAINT products_kind_chk CHECK ((kind = ANY (ARRAY['lead_magnet'::text, 'tripwire'::text, 'main'::text]))),
    CONSTRAINT products_status_chk CHECK ((status = ANY (ARRAY['active'::text, 'archived'::text])))
);


--
-- Name: products_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.products_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: products_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.products_id_seq OWNED BY public.products.id;


--
-- Name: prospects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.prospects (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    inn text NOT NULL,
    kpp text,
    ogrn text,
    subject_type text DEFAULT 'legal'::text NOT NULL,
    name_short text,
    name_full text,
    opf text,
    okved text,
    okved_name text,
    okveds jsonb,
    address text,
    region text,
    city text,
    status text,
    registration_date date,
    liquidation_date date,
    management jsonb,
    lead_id uuid,
    source text DEFAULT 'dadata'::text NOT NULL,
    raw jsonb,
    fetched_at timestamp with time zone,
    archived boolean DEFAULT false NOT NULL,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT prospects_subject_type_check CHECK ((subject_type = ANY (ARRAY['legal'::text, 'individual'::text])))
);


--
-- Name: service_invoices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.service_invoices (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    plan_key text NOT NULL,
    plan_name text NOT NULL,
    quota integer,
    plan_amount numeric(12,2) NOT NULL,
    overage_count integer DEFAULT 0 NOT NULL,
    overage_amount numeric(12,2) DEFAULT 0 NOT NULL,
    amount numeric(12,2) NOT NULL,
    currency text DEFAULT 'RUB'::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    yookassa_payment_id text,
    card_last4 text,
    paid_at timestamp with time zone,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid NOT NULL,
    CONSTRAINT service_invoices_amount_chk CHECK ((amount >= (0)::numeric)),
    CONSTRAINT service_invoices_status_chk CHECK ((status = ANY (ARRAY['pending'::text, 'paid'::text, 'canceled'::text])))
);


--
-- Name: subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscriptions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    status text DEFAULT 'trialing'::text NOT NULL,
    current_period_start timestamp with time zone NOT NULL,
    current_period_end timestamp with time zone NOT NULL,
    yookassa_payment_method_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    receipt_email text,
    last_charge_attempt_at timestamp with time zone,
    charge_attempts integer DEFAULT 0 NOT NULL,
    CONSTRAINT subscriptions_status_check CHECK ((status = ANY (ARRAY['trialing'::text, 'active'::text, 'past_due'::text, 'canceled'::text])))
);


--
-- Name: team_agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.team_agents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    slug text NOT NULL,
    name text DEFAULT ''::text NOT NULL,
    role_preset text,
    system_prompt text DEFAULT ''::text NOT NULL,
    backend text,
    agent_id text DEFAULT ''::text NOT NULL,
    fallback_text text DEFAULT ''::text NOT NULL,
    escalation_chat_id text DEFAULT ''::text NOT NULL,
    escalation_topic_id integer,
    is_default boolean DEFAULT false NOT NULL,
    is_orchestrator boolean DEFAULT false NOT NULL,
    memory_enabled boolean DEFAULT false NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    "position" integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    kb_enabled boolean DEFAULT true NOT NULL
);


--
-- Name: tenant_agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_agents (
    agent_id bigint NOT NULL,
    tenant_id uuid NOT NULL,
    access_id text,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: tenant_brief; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_brief (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    token text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    answers jsonb,
    proposal jsonb,
    applied jsonb,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    submitted_at timestamp with time zone,
    proposed_at timestamp with time zone,
    applied_at timestamp with time zone,
    expires_at timestamp with time zone,
    CONSTRAINT tenant_brief_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'submitted'::text, 'proposed'::text, 'applied'::text, 'expired'::text])))
);


--
-- Name: tenant_secrets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_secrets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    key_name text NOT NULL,
    ciphertext bytea NOT NULL,
    nonce bytea NOT NULL,
    key_version integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone
);


--
-- Name: tenant_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_settings (
    tenant_id uuid NOT NULL,
    key text NOT NULL,
    value text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: tenant_triggers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_triggers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    channel text DEFAULT 'telegram'::text NOT NULL,
    type text NOT NULL,
    action text DEFAULT 'notify_reply_continue'::text NOT NULL,
    stopwords text[] DEFAULT '{}'::text[] NOT NULL,
    intent_desc text DEFAULT ''::text NOT NULL,
    msg_count integer,
    notify_chat_id text DEFAULT ''::text NOT NULL,
    notify_topic_id integer,
    reply_text text DEFAULT ''::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    "position" integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tenant_triggers_action_check CHECK ((action = ANY (ARRAY['notify_reply_continue'::text, 'notify_reply_pause'::text, 'notify_only'::text]))),
    CONSTRAINT tenant_triggers_type_check CHECK ((type = ANY (ARRAY['stopwords'::text, 'intent'::text, 'message_count'::text, 'documents'::text])))
);


--
-- Name: tenants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    status text DEFAULT 'provisioning'::text NOT NULL,
    plan_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    partner_id uuid,
    ref_tg_user_id bigint,
    CONSTRAINT tenants_status_check CHECK ((status = ANY (ARRAY['provisioning'::text, 'active'::text, 'suspended'::text, 'canceled'::text])))
);


--
-- Name: usage_ledger; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_ledger (
    id bigint NOT NULL,
    tenant_id uuid NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    kind text NOT NULL,
    provider text,
    model text,
    units jsonb DEFAULT '{}'::jsonb NOT NULL,
    cost_microrub bigint NOT NULL,
    multiplier numeric(4,2) NOT NULL,
    charged_microrub bigint NOT NULL,
    balance_after_microrub bigint NOT NULL,
    request_id text,
    idempotence_key text NOT NULL,
    CONSTRAINT usage_ledger_kind_check CHECK ((kind = ANY (ARRAY['llm'::text, 'embedding'::text, 'message'::text, 'other'::text])))
);


--
-- Name: usage_ledger_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.usage_ledger ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.usage_ledger_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: webhook_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider text DEFAULT 'yookassa'::text NOT NULL,
    external_id text NOT NULL,
    event_type text,
    payload jsonb,
    status text DEFAULT 'received'::text NOT NULL,
    processed_at timestamp with time zone,
    CONSTRAINT webhook_events_status_check CHECK ((status = ANY (ARRAY['received'::text, 'processed'::text, 'failed'::text])))
);


--
-- Name: admin_audit id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_audit ALTER COLUMN id SET DEFAULT nextval('public.admin_audit_id_seq'::regclass);


--
-- Name: broadcast_files id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_files ALTER COLUMN id SET DEFAULT nextval('public.broadcast_files_id_seq'::regclass);


--
-- Name: broadcast_recipients id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_recipients ALTER COLUMN id SET DEFAULT nextval('public.broadcast_recipients_id_seq'::regclass);


--
-- Name: broadcasts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcasts ALTER COLUMN id SET DEFAULT nextval('public.broadcasts_id_seq'::regclass);


--
-- Name: consent_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consent_events ALTER COLUMN id SET DEFAULT nextval('public.consent_events_id_seq'::regclass);


--
-- Name: link_clicks id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.link_clicks ALTER COLUMN id SET DEFAULT nextval('public.link_clicks_id_seq'::regclass);


--
-- Name: messages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages ALTER COLUMN id SET DEFAULT nextval('public.messages_id_seq'::regclass);


--
-- Name: outbox id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outbox ALTER COLUMN id SET DEFAULT nextval('public.outbox_id_seq'::regclass);


--
-- Name: platform_notify id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_notify ALTER COLUMN id SET DEFAULT nextval('public.platform_notify_id_seq'::regclass);


--
-- Name: products id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products ALTER COLUMN id SET DEFAULT nextval('public.products_id_seq'::regclass);


--
-- Name: account_identities account_identities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_identities
    ADD CONSTRAINT account_identities_pkey PRIMARY KEY (id);


--
-- Name: account_identities account_identities_provider_external_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_identities
    ADD CONSTRAINT account_identities_provider_external_id_key UNIQUE (provider, external_id);


--
-- Name: admin_audit admin_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_audit
    ADD CONSTRAINT admin_audit_pkey PRIMARY KEY (id);


--
-- Name: admin_login_throttle admin_login_throttle_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_login_throttle
    ADD CONSTRAINT admin_login_throttle_pkey PRIMARY KEY (account);


--
-- Name: admin_sessions admin_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_sessions
    ADD CONSTRAINT admin_sessions_pkey PRIMARY KEY (sid);


--
-- Name: admin_users admin_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_users
    ADD CONSTRAINT admin_users_pkey PRIMARY KEY (username);


--
-- Name: agent_memory agent_memory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_memory
    ADD CONSTRAINT agent_memory_pkey PRIMARY KEY (id);


--
-- Name: agent_token_snapshots agent_token_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_token_snapshots
    ADD CONSTRAINT agent_token_snapshots_pkey PRIMARY KEY (agent_id, taken_at);


--
-- Name: app_settings app_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_settings
    ADD CONSTRAINT app_settings_pkey PRIMARY KEY (key);


--
-- Name: broadcast_files broadcast_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_files
    ADD CONSTRAINT broadcast_files_pkey PRIMARY KEY (id);


--
-- Name: broadcast_recipients broadcast_recipients_broadcast_id_lead_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_recipients
    ADD CONSTRAINT broadcast_recipients_broadcast_id_lead_id_key UNIQUE (broadcast_id, lead_id);


--
-- Name: broadcast_recipients broadcast_recipients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_recipients
    ADD CONSTRAINT broadcast_recipients_pkey PRIMARY KEY (id);


--
-- Name: broadcasts broadcasts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcasts
    ADD CONSTRAINT broadcasts_pkey PRIMARY KEY (id);


--
-- Name: club_intros club_intros_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.club_intros
    ADD CONSTRAINT club_intros_pkey PRIMARY KEY (id);


--
-- Name: club_members club_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.club_members
    ADD CONSTRAINT club_members_pkey PRIMARY KEY (id);


--
-- Name: club_profiles club_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.club_profiles
    ADD CONSTRAINT club_profiles_pkey PRIMARY KEY (member_id);


--
-- Name: consent_events consent_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consent_events
    ADD CONSTRAINT consent_events_pkey PRIMARY KEY (id);


--
-- Name: credit_wallets credit_wallets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_wallets
    ADD CONSTRAINT credit_wallets_pkey PRIMARY KEY (tenant_id);


--
-- Name: kb_chunks kb_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_chunks
    ADD CONSTRAINT kb_chunks_pkey PRIMARY KEY (id);


--
-- Name: kb_documents kb_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_documents
    ADD CONSTRAINT kb_documents_pkey PRIMARY KEY (id);


--
-- Name: leads leads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_pkey PRIMARY KEY (id);


--
-- Name: link_clicks link_clicks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.link_clicks
    ADD CONSTRAINT link_clicks_pkey PRIMARY KEY (id);


--
-- Name: link_tokens link_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.link_tokens
    ADD CONSTRAINT link_tokens_pkey PRIMARY KEY (token);


--
-- Name: memberships memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_pkey PRIMARY KEY (id);


--
-- Name: memberships memberships_tenant_id_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_tenant_id_username_key UNIQUE (tenant_id, username);


--
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);


--
-- Name: model_prices model_prices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_prices
    ADD CONSTRAINT model_prices_pkey PRIMARY KEY (id);


--
-- Name: model_prices model_prices_provider_model_effective_from_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_prices
    ADD CONSTRAINT model_prices_provider_model_effective_from_key UNIQUE (provider, model, effective_from);


--
-- Name: orders orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);


--
-- Name: outbox outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outbox
    ADD CONSTRAINT outbox_pkey PRIMARY KEY (id);


--
-- Name: partners partners_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.partners
    ADD CONSTRAINT partners_pkey PRIMARY KEY (id);


--
-- Name: partners partners_ref_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.partners
    ADD CONSTRAINT partners_ref_code_key UNIQUE (ref_code);


--
-- Name: password_reset_tokens password_reset_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_pkey PRIMARY KEY (token_hash);


--
-- Name: payments payments_idempotence_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_idempotence_key_key UNIQUE (idempotence_key);


--
-- Name: payments payments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_pkey PRIMARY KEY (id);


--
-- Name: payments payments_yookassa_payment_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_yookassa_payment_id_key UNIQUE (yookassa_payment_id);


--
-- Name: plans plans_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plans
    ADD CONSTRAINT plans_code_key UNIQUE (code);


--
-- Name: plans plans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plans
    ADD CONSTRAINT plans_pkey PRIMARY KEY (id);


--
-- Name: platform_notify platform_notify_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_notify
    ADD CONSTRAINT platform_notify_pkey PRIMARY KEY (id);


--
-- Name: products products_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_pkey PRIMARY KEY (id);


--
-- Name: prospects prospects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prospects
    ADD CONSTRAINT prospects_pkey PRIMARY KEY (id);


--
-- Name: prospects prospects_tenant_id_inn_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prospects
    ADD CONSTRAINT prospects_tenant_id_inn_key UNIQUE (tenant_id, inn);


--
-- Name: service_invoices service_invoices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_invoices
    ADD CONSTRAINT service_invoices_pkey PRIMARY KEY (id);


--
-- Name: subscriptions subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_pkey PRIMARY KEY (id);


--
-- Name: team_agents team_agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_agents
    ADD CONSTRAINT team_agents_pkey PRIMARY KEY (id);


--
-- Name: team_agents team_agents_tenant_id_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_agents
    ADD CONSTRAINT team_agents_tenant_id_slug_key UNIQUE (tenant_id, slug);


--
-- Name: tenant_agents tenant_agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_agents
    ADD CONSTRAINT tenant_agents_pkey PRIMARY KEY (agent_id);


--
-- Name: tenant_brief tenant_brief_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_brief
    ADD CONSTRAINT tenant_brief_pkey PRIMARY KEY (id);


--
-- Name: tenant_brief tenant_brief_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_brief
    ADD CONSTRAINT tenant_brief_token_key UNIQUE (token);


--
-- Name: tenant_secrets tenant_secrets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_secrets
    ADD CONSTRAINT tenant_secrets_pkey PRIMARY KEY (id);


--
-- Name: tenant_secrets tenant_secrets_tenant_id_key_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_secrets
    ADD CONSTRAINT tenant_secrets_tenant_id_key_name_key UNIQUE (tenant_id, key_name);


--
-- Name: tenant_settings tenant_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_settings
    ADD CONSTRAINT tenant_settings_pkey PRIMARY KEY (tenant_id, key);


--
-- Name: tenant_triggers tenant_triggers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_triggers
    ADD CONSTRAINT tenant_triggers_pkey PRIMARY KEY (id);


--
-- Name: tenants tenants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id);


--
-- Name: tenants tenants_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_slug_key UNIQUE (slug);


--
-- Name: usage_ledger usage_ledger_idempotence_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_ledger
    ADD CONSTRAINT usage_ledger_idempotence_key_key UNIQUE (idempotence_key);


--
-- Name: usage_ledger usage_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_ledger
    ADD CONSTRAINT usage_ledger_pkey PRIMARY KEY (id);


--
-- Name: webhook_events webhook_events_external_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_events
    ADD CONSTRAINT webhook_events_external_id_key UNIQUE (external_id);


--
-- Name: webhook_events webhook_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_events
    ADD CONSTRAINT webhook_events_pkey PRIMARY KEY (id);


--
-- Name: account_identities_username_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX account_identities_username_idx ON public.account_identities USING btree (username);


--
-- Name: admin_audit_action_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX admin_audit_action_idx ON public.admin_audit USING btree (action);


--
-- Name: admin_audit_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX admin_audit_at_idx ON public.admin_audit USING btree (at DESC);


--
-- Name: admin_audit_lead_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX admin_audit_lead_id_idx ON public.admin_audit USING btree (lead_id);


--
-- Name: admin_sessions_actor_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX admin_sessions_actor_idx ON public.admin_sessions USING btree (actor) WHERE (revoked = false);


--
-- Name: admin_sessions_expires_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX admin_sessions_expires_idx ON public.admin_sessions USING btree (expires_at) WHERE (revoked = false);


--
-- Name: admin_users_active_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX admin_users_active_idx ON public.admin_users USING btree (active, created_at DESC);


--
-- Name: agent_memory_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_memory_embedding_idx ON public.agent_memory USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: agent_memory_meta_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_memory_meta_idx ON public.agent_memory USING gin (metadata jsonb_path_ops);


--
-- Name: agent_memory_scope_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_memory_scope_idx ON public.agent_memory USING btree (tenant_id, agent_id);


--
-- Name: agent_snapshots_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_snapshots_tenant_idx ON public.agent_token_snapshots USING btree (tenant_id, taken_at DESC);


--
-- Name: br_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX br_pending_idx ON public.broadcast_recipients USING btree (broadcast_id) WHERE (status = 'pending'::text);


--
-- Name: br_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX br_status_idx ON public.broadcast_recipients USING btree (broadcast_id, status);


--
-- Name: broadcast_files_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX broadcast_files_tenant_idx ON public.broadcast_files USING btree (tenant_id, created_at);


--
-- Name: broadcast_recipients_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX broadcast_recipients_tenant_idx ON public.broadcast_recipients USING btree (tenant_id, broadcast_id);


--
-- Name: broadcasts_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX broadcasts_created_idx ON public.broadcasts USING btree (created_at DESC);


--
-- Name: broadcasts_product_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX broadcasts_product_idx ON public.broadcasts USING btree (product_id) WHERE (product_id IS NOT NULL);


--
-- Name: broadcasts_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX broadcasts_tenant_idx ON public.broadcasts USING btree (tenant_id, created_at);


--
-- Name: club_intros_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX club_intros_tenant_idx ON public.club_intros USING btree (tenant_id, status, created_at DESC);


--
-- Name: club_members_erase_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX club_members_erase_idx ON public.club_members USING btree (tenant_id, erase_requested_at);


--
-- Name: club_members_tenant_city_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX club_members_tenant_city_idx ON public.club_members USING btree (tenant_id, city, okved);


--
-- Name: club_members_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX club_members_tenant_idx ON public.club_members USING btree (tenant_id, status);


--
-- Name: club_members_tenant_lead_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX club_members_tenant_lead_idx ON public.club_members USING btree (tenant_id, lead_id);


--
-- Name: club_members_tenant_tg_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX club_members_tenant_tg_idx ON public.club_members USING btree (tenant_id, tg_user_id);


--
-- Name: consent_events_tenant_action_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX consent_events_tenant_action_idx ON public.consent_events USING btree (tenant_id, action, occurred_at DESC);


--
-- Name: consent_events_tenant_lead_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX consent_events_tenant_lead_idx ON public.consent_events USING btree (tenant_id, lead_id);


--
-- Name: kb_chunks_doc_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX kb_chunks_doc_idx ON public.kb_chunks USING btree (document_id);


--
-- Name: kb_chunks_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX kb_chunks_embedding_idx ON public.kb_chunks USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: kb_chunks_meta_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX kb_chunks_meta_idx ON public.kb_chunks USING gin (metadata jsonb_path_ops);


--
-- Name: kb_chunks_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX kb_chunks_tenant_idx ON public.kb_chunks USING btree (tenant_id, document_id);


--
-- Name: kb_documents_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX kb_documents_tenant_idx ON public.kb_documents USING btree (tenant_id, created_at);


--
-- Name: leads_bot_paused_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leads_bot_paused_idx ON public.leads USING btree (bot_paused) WHERE (bot_paused = true);


--
-- Name: leads_created_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leads_created_at_idx ON public.leads USING btree (created_at DESC);


--
-- Name: leads_erase_requested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leads_erase_requested_at_idx ON public.leads USING btree (erase_requested_at) WHERE (erase_requested_at IS NOT NULL);


--
-- Name: leads_messenger_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leads_messenger_idx ON public.leads USING btree (messenger);


--
-- Name: leads_phone_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leads_phone_hash_idx ON public.leads USING btree (phone_hash);


--
-- Name: leads_source_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leads_source_idx ON public.leads USING btree (source);


--
-- Name: leads_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leads_status_idx ON public.leads USING btree (status);


--
-- Name: leads_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leads_tenant_idx ON public.leads USING btree (tenant_id, created_at);


--
-- Name: leads_tenant_max_user_id_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX leads_tenant_max_user_id_key ON public.leads USING btree (tenant_id, max_user_id);


--
-- Name: leads_tenant_tg_user_id_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX leads_tenant_tg_user_id_key ON public.leads USING btree (tenant_id, tg_user_id);


--
-- Name: leads_tenant_vk_user_id_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX leads_tenant_vk_user_id_key ON public.leads USING btree (tenant_id, vk_user_id);


--
-- Name: leads_tenant_web_session_id_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX leads_tenant_web_session_id_key ON public.leads USING btree (tenant_id, web_session_id);


--
-- Name: leads_unsubscribed_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leads_unsubscribed_idx ON public.leads USING btree (unsubscribed_at) WHERE (unsubscribed_at IS NOT NULL);


--
-- Name: link_clicks_broadcast_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX link_clicks_broadcast_idx ON public.link_clicks USING btree (broadcast_id);


--
-- Name: link_clicks_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX link_clicks_tenant_idx ON public.link_clicks USING btree (tenant_id, clicked_at);


--
-- Name: link_clicks_token_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX link_clicks_token_idx ON public.link_clicks USING btree (token);


--
-- Name: link_tokens_broadcast_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX link_tokens_broadcast_idx ON public.link_tokens USING btree (broadcast_id);


--
-- Name: link_tokens_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX link_tokens_tenant_idx ON public.link_tokens USING btree (tenant_id, created_at);


--
-- Name: memberships_username_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memberships_username_idx ON public.memberships USING btree (username);


--
-- Name: messages_lead_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX messages_lead_created_idx ON public.messages USING btree (lead_id, created_at);


--
-- Name: messages_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX messages_tenant_idx ON public.messages USING btree (tenant_id, created_at);


--
-- Name: messages_tg_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX messages_tg_created_idx ON public.messages USING btree (tg_user_id, created_at);


--
-- Name: orders_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_created_idx ON public.orders USING btree (created_at DESC);


--
-- Name: orders_lead_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_lead_idx ON public.orders USING btree (lead_id);


--
-- Name: orders_provider_payment_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_provider_payment_idx ON public.orders USING btree (provider_payment_id) WHERE (provider_payment_id IS NOT NULL);


--
-- Name: orders_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_status_idx ON public.orders USING btree (status);


--
-- Name: orders_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_tenant_idx ON public.orders USING btree (tenant_id, created_at);


--
-- Name: outbox_pending_upload_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX outbox_pending_upload_idx ON public.outbox USING btree (id) WHERE ((file_bytes IS NOT NULL) AND (file_id IS NULL));


--
-- Name: outbox_queued_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX outbox_queued_idx ON public.outbox USING btree (created_at) WHERE (status = 'queued'::text);


--
-- Name: outbox_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX outbox_tenant_idx ON public.outbox USING btree (tenant_id, created_at);


--
-- Name: password_reset_tokens_expires_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX password_reset_tokens_expires_idx ON public.password_reset_tokens USING btree (expires_at);


--
-- Name: password_reset_tokens_username_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX password_reset_tokens_username_idx ON public.password_reset_tokens USING btree (username, created_at DESC);


--
-- Name: payments_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payments_tenant_idx ON public.payments USING btree (tenant_id, created_at DESC);


--
-- Name: platform_notify_queued_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX platform_notify_queued_idx ON public.platform_notify USING btree (created_at) WHERE (status = 'queued'::text);


--
-- Name: products_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX products_created_idx ON public.products USING btree (created_at DESC);


--
-- Name: products_pending_upload_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX products_pending_upload_idx ON public.products USING btree (id) WHERE ((file IS NOT NULL) AND (file_tg_id IS NULL));


--
-- Name: products_status_kind_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX products_status_kind_idx ON public.products USING btree (status, kind);


--
-- Name: products_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX products_tenant_idx ON public.products USING btree (tenant_id, created_at);


--
-- Name: prospects_tenant_city_okved_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX prospects_tenant_city_okved_idx ON public.prospects USING btree (tenant_id, city, okved);


--
-- Name: prospects_tenant_lead_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX prospects_tenant_lead_idx ON public.prospects USING btree (tenant_id, lead_id);


--
-- Name: service_invoices_period_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX service_invoices_period_idx ON public.service_invoices USING btree (period_end DESC);


--
-- Name: service_invoices_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX service_invoices_status_idx ON public.service_invoices USING btree (status);


--
-- Name: service_invoices_tenant_period_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX service_invoices_tenant_period_idx ON public.service_invoices USING btree (tenant_id, period_end DESC);


--
-- Name: service_invoices_ykid_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX service_invoices_ykid_idx ON public.service_invoices USING btree (yookassa_payment_id) WHERE (yookassa_payment_id IS NOT NULL);


--
-- Name: subscriptions_period_end_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX subscriptions_period_end_idx ON public.subscriptions USING btree (current_period_end) WHERE (status = ANY (ARRAY['trialing'::text, 'active'::text]));


--
-- Name: subscriptions_renewal_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX subscriptions_renewal_idx ON public.subscriptions USING btree (current_period_end) WHERE ((status = ANY (ARRAY['active'::text, 'past_due'::text])) AND (yookassa_payment_method_id IS NOT NULL));


--
-- Name: subscriptions_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX subscriptions_tenant_idx ON public.subscriptions USING btree (tenant_id, created_at DESC);


--
-- Name: team_agents_lookup_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX team_agents_lookup_idx ON public.team_agents USING btree (tenant_id, enabled);


--
-- Name: team_agents_one_default_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX team_agents_one_default_idx ON public.team_agents USING btree (tenant_id) WHERE is_default;


--
-- Name: tenant_agents_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_agents_tenant_idx ON public.tenant_agents USING btree (tenant_id);


--
-- Name: tenant_brief_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_brief_status_idx ON public.tenant_brief USING btree (status);


--
-- Name: tenant_brief_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_brief_tenant_idx ON public.tenant_brief USING btree (tenant_id);


--
-- Name: tenant_triggers_lookup_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_triggers_lookup_idx ON public.tenant_triggers USING btree (tenant_id, enabled, type);


--
-- Name: tenants_partner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenants_partner_idx ON public.tenants USING btree (partner_id) WHERE (partner_id IS NOT NULL);


--
-- Name: tenants_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenants_status_idx ON public.tenants USING btree (status);


--
-- Name: usage_ledger_tenant_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_ledger_tenant_idx ON public.usage_ledger USING btree (tenant_id, occurred_at DESC);


--
-- Name: app_settings trg_app_settings_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_app_settings_updated_at BEFORE UPDATE ON public.app_settings FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: leads trg_leads_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_leads_updated_at BEFORE UPDATE ON public.leads FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: products trg_products_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_products_updated_at BEFORE UPDATE ON public.products FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: account_identities account_identities_username_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_identities
    ADD CONSTRAINT account_identities_username_fkey FOREIGN KEY (username) REFERENCES public.admin_users(username) ON DELETE CASCADE;


--
-- Name: admin_audit admin_audit_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_audit
    ADD CONSTRAINT admin_audit_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE SET NULL;


--
-- Name: admin_sessions admin_sessions_active_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_sessions
    ADD CONSTRAINT admin_sessions_active_tenant_id_fkey FOREIGN KEY (active_tenant_id) REFERENCES public.tenants(id);


--
-- Name: agent_memory agent_memory_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_memory
    ADD CONSTRAINT agent_memory_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.team_agents(id) ON DELETE CASCADE;


--
-- Name: agent_memory agent_memory_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_memory
    ADD CONSTRAINT agent_memory_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: agent_token_snapshots agent_token_snapshots_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_token_snapshots
    ADD CONSTRAINT agent_token_snapshots_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: broadcast_files broadcast_files_broadcast_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_files
    ADD CONSTRAINT broadcast_files_broadcast_id_fkey FOREIGN KEY (broadcast_id) REFERENCES public.broadcasts(id) ON DELETE CASCADE;


--
-- Name: broadcast_files broadcast_files_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_files
    ADD CONSTRAINT broadcast_files_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: broadcast_recipients broadcast_recipients_broadcast_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_recipients
    ADD CONSTRAINT broadcast_recipients_broadcast_id_fkey FOREIGN KEY (broadcast_id) REFERENCES public.broadcasts(id) ON DELETE CASCADE;


--
-- Name: broadcast_recipients broadcast_recipients_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_recipients
    ADD CONSTRAINT broadcast_recipients_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;


--
-- Name: broadcast_recipients broadcast_recipients_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_recipients
    ADD CONSTRAINT broadcast_recipients_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: broadcasts broadcasts_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcasts
    ADD CONSTRAINT broadcasts_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE SET NULL;


--
-- Name: broadcasts broadcasts_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcasts
    ADD CONSTRAINT broadcasts_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: club_intros club_intros_from_member_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.club_intros
    ADD CONSTRAINT club_intros_from_member_fkey FOREIGN KEY (from_member) REFERENCES public.club_members(id) ON DELETE CASCADE;


--
-- Name: club_intros club_intros_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.club_intros
    ADD CONSTRAINT club_intros_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: club_intros club_intros_to_member_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.club_intros
    ADD CONSTRAINT club_intros_to_member_fkey FOREIGN KEY (to_member) REFERENCES public.club_members(id) ON DELETE CASCADE;


--
-- Name: club_intros club_intros_to_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.club_intros
    ADD CONSTRAINT club_intros_to_tenant_id_fkey FOREIGN KEY (to_tenant_id) REFERENCES public.tenants(id) ON DELETE SET NULL;


--
-- Name: club_members club_members_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.club_members
    ADD CONSTRAINT club_members_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE SET NULL;


--
-- Name: club_members club_members_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.club_members
    ADD CONSTRAINT club_members_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: club_profiles club_profiles_member_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.club_profiles
    ADD CONSTRAINT club_profiles_member_id_fkey FOREIGN KEY (member_id) REFERENCES public.club_members(id) ON DELETE CASCADE;


--
-- Name: club_profiles club_profiles_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.club_profiles
    ADD CONSTRAINT club_profiles_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: consent_events consent_events_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consent_events
    ADD CONSTRAINT consent_events_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE SET NULL;


--
-- Name: consent_events consent_events_member_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consent_events
    ADD CONSTRAINT consent_events_member_id_fkey FOREIGN KEY (member_id) REFERENCES public.club_members(id) ON DELETE SET NULL;


--
-- Name: consent_events consent_events_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consent_events
    ADD CONSTRAINT consent_events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: credit_wallets credit_wallets_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_wallets
    ADD CONSTRAINT credit_wallets_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: kb_chunks kb_chunks_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_chunks
    ADD CONSTRAINT kb_chunks_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.kb_documents(id) ON DELETE CASCADE;


--
-- Name: kb_chunks kb_chunks_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_chunks
    ADD CONSTRAINT kb_chunks_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: kb_documents kb_documents_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_documents
    ADD CONSTRAINT kb_documents_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: leads leads_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: link_clicks link_clicks_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.link_clicks
    ADD CONSTRAINT link_clicks_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE SET NULL;


--
-- Name: link_clicks link_clicks_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.link_clicks
    ADD CONSTRAINT link_clicks_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: link_clicks link_clicks_token_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.link_clicks
    ADD CONSTRAINT link_clicks_token_fkey FOREIGN KEY (token) REFERENCES public.link_tokens(token) ON DELETE CASCADE;


--
-- Name: link_tokens link_tokens_broadcast_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.link_tokens
    ADD CONSTRAINT link_tokens_broadcast_id_fkey FOREIGN KEY (broadcast_id) REFERENCES public.broadcasts(id) ON DELETE CASCADE;


--
-- Name: link_tokens link_tokens_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.link_tokens
    ADD CONSTRAINT link_tokens_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE SET NULL;


--
-- Name: link_tokens link_tokens_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.link_tokens
    ADD CONSTRAINT link_tokens_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: memberships memberships_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: memberships memberships_username_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_username_fkey FOREIGN KEY (username) REFERENCES public.admin_users(username) ON DELETE CASCADE;


--
-- Name: messages messages_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;


--
-- Name: messages messages_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: orders orders_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE SET NULL;


--
-- Name: orders orders_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE SET NULL;


--
-- Name: orders orders_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: outbox outbox_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outbox
    ADD CONSTRAINT outbox_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;


--
-- Name: outbox outbox_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outbox
    ADD CONSTRAINT outbox_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: password_reset_tokens password_reset_tokens_username_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_username_fkey FOREIGN KEY (username) REFERENCES public.admin_users(username) ON DELETE CASCADE;


--
-- Name: payments payments_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: products products_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: prospects prospects_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prospects
    ADD CONSTRAINT prospects_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE SET NULL;


--
-- Name: prospects prospects_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prospects
    ADD CONSTRAINT prospects_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: service_invoices service_invoices_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_invoices
    ADD CONSTRAINT service_invoices_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: subscriptions subscriptions_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.plans(id);


--
-- Name: subscriptions subscriptions_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: team_agents team_agents_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_agents
    ADD CONSTRAINT team_agents_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: tenant_agents tenant_agents_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_agents
    ADD CONSTRAINT tenant_agents_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: tenant_brief tenant_brief_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_brief
    ADD CONSTRAINT tenant_brief_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: tenant_secrets tenant_secrets_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_secrets
    ADD CONSTRAINT tenant_secrets_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: tenant_settings tenant_settings_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_settings
    ADD CONSTRAINT tenant_settings_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: tenant_triggers tenant_triggers_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_triggers
    ADD CONSTRAINT tenant_triggers_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: tenants tenants_partner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES public.partners(id);


--
-- Name: tenants tenants_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.plans(id);


--
-- Name: usage_ledger usage_ledger_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_ledger
    ADD CONSTRAINT usage_ledger_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: agent_memory; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_memory ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_token_snapshots; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_token_snapshots ENABLE ROW LEVEL SECURITY;

--
-- Name: broadcast_files; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.broadcast_files ENABLE ROW LEVEL SECURITY;

--
-- Name: broadcast_recipients; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.broadcast_recipients ENABLE ROW LEVEL SECURITY;

--
-- Name: broadcasts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.broadcasts ENABLE ROW LEVEL SECURITY;

--
-- Name: club_intros; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.club_intros ENABLE ROW LEVEL SECURITY;

--
-- Name: club_members; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.club_members ENABLE ROW LEVEL SECURITY;

--
-- Name: club_profiles; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.club_profiles ENABLE ROW LEVEL SECURITY;

--
-- Name: consent_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.consent_events ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_wallets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.credit_wallets ENABLE ROW LEVEL SECURITY;

--
-- Name: kb_chunks; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.kb_chunks ENABLE ROW LEVEL SECURITY;

--
-- Name: kb_documents; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.kb_documents ENABLE ROW LEVEL SECURITY;

--
-- Name: leads; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.leads ENABLE ROW LEVEL SECURITY;

--
-- Name: link_clicks; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.link_clicks ENABLE ROW LEVEL SECURITY;

--
-- Name: link_tokens; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.link_tokens ENABLE ROW LEVEL SECURITY;

--
-- Name: messages; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.messages ENABLE ROW LEVEL SECURITY;

--
-- Name: orders; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.orders ENABLE ROW LEVEL SECURITY;

--
-- Name: outbox; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.outbox ENABLE ROW LEVEL SECURITY;

--
-- Name: payments; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.payments ENABLE ROW LEVEL SECURITY;

--
-- Name: prospects; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.prospects ENABLE ROW LEVEL SECURITY;

--
-- Name: service_invoices; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.service_invoices ENABLE ROW LEVEL SECURITY;

--
-- Name: subscriptions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;

--
-- Name: team_agents; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.team_agents ENABLE ROW LEVEL SECURITY;

--
-- Name: tenant_agents; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.tenant_agents ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_memory tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.agent_memory USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: agent_token_snapshots tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.agent_token_snapshots USING ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid));


--
-- Name: broadcast_files tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.broadcast_files USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: broadcast_recipients tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.broadcast_recipients USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: broadcasts tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.broadcasts USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: club_intros tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.club_intros USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: club_members tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.club_members USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: club_profiles tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.club_profiles USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: consent_events tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.consent_events USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: credit_wallets tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.credit_wallets USING ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid));


--
-- Name: kb_chunks tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.kb_chunks USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: kb_documents tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.kb_documents USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: leads tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.leads USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: link_clicks tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.link_clicks USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: link_tokens tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.link_tokens USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: messages tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.messages USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: orders tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.orders USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: outbox tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.outbox USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: payments tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.payments USING ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid));


--
-- Name: prospects tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.prospects USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: service_invoices tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.service_invoices USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: subscriptions tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.subscriptions USING ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid));


--
-- Name: team_agents tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.team_agents USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: tenant_agents tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.tenant_agents USING ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid));


--
-- Name: tenant_secrets tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.tenant_secrets USING ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid));


--
-- Name: tenant_settings tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.tenant_settings USING ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid));


--
-- Name: tenant_triggers tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.tenant_triggers USING ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid));


--
-- Name: usage_ledger tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.usage_ledger USING ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.tenant_id'::text, true))::uuid));


--
-- Name: tenant_secrets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.tenant_secrets ENABLE ROW LEVEL SECURITY;

--
-- Name: tenant_settings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.tenant_settings ENABLE ROW LEVEL SECURITY;

--
-- Name: tenant_triggers; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.tenant_triggers ENABLE ROW LEVEL SECURITY;

--
-- Name: usage_ledger; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.usage_ledger ENABLE ROW LEVEL SECURITY;

--
-- PostgreSQL database dump complete
--

\unrestrict gcIqSccRRU0DgziJf95Z9Q9mi0HlGXbmegl2HmZJbnQmfplF6R2y0BUZGxL7Ud2

