-- 工地監工 Pro — Supabase 資料庫初始化腳本
-- 用法：登入 Supabase Dashboard → 你的專案 → SQL Editor → New query → 貼上整段 → Run
--
-- 架構：文字紀錄存這裡（Supabase Postgres），照片存在你自己的 Google Drive，
-- 這裡只存 Google Drive 檔案的 file_id 當索引，不存照片本身。
--
-- 案場（專案）先建立一次，之後每筆缺失紀錄都歸在案場底下（records.site_id）。
-- 一筆缺失紀錄可以對應多張照片（record_photos），每張各自可以有標註圖。
--
-- 注意：這裡改動了資料表結構，直接砍掉 records/record_photos 重建最乾淨；
-- 如果你已經有正式資料要保留，請先自行備份、轉換格式，不要直接執行這份腳本。
drop table if exists public.record_photos;
drop table if exists public.records;

create table if not exists public.sites (
    id               bigint generated always as identity primary key,
    name             text        not null unique,
    created_at       timestamptz not null default now()
);

create table if not exists public.records (
    id                  bigint generated always as identity primary key,
    site_id             bigint      not null references public.sites(id) on delete cascade,
    record_date         date        not null,
    trade               text        not null,
    vendor              text        not null,
    status              text        not null default '待處理'
                            check (status in ('待處理', '修繕中', '已驗收')),
    note                text        default '',
    created_at          timestamptz not null default now()
);

create table if not exists public.record_photos (
    id                  bigint generated always as identity primary key,
    record_id           bigint      not null references public.records(id) on delete cascade,
    photo_file_id       text        not null,   -- Google Drive 原圖 file id
    annotated_file_id   text,                    -- Google Drive 標註圖 file id，可為 NULL
    sort_order          int         not null default 0,
    created_at          timestamptz not null default now()
);

create index if not exists records_site_idx        on public.records (site_id);
create index if not exists records_vendor_idx      on public.records (vendor);
create index if not exists records_status_idx      on public.records (status);
create index if not exists records_date_idx        on public.records (record_date desc);
create index if not exists record_photos_record_idx on public.record_photos (record_id);

-- 啟用 Row Level Security，但不新增任何 public 存取政策。
-- App 後端一律使用 service_role 金鑰連線，service_role 會自動略過 RLS，
-- 所以其他人即使拿到你的 Supabase URL 也讀不到任何資料。
alter table public.sites          enable row level security;
alter table public.records        enable row level security;
alter table public.record_photos  enable row level security;

-- GRANT 是獨立於 RLS 之外的另一層權限檢查，即使 service_role 會略過 RLS，
-- 沒有這幾行 grant 一樣會拿到 "permission denied for table ..." 錯誤
-- （尤其是專案建立時如果關掉了 Dashboard 的「Automatically expose new tables」）。
grant usage on schema public to service_role;
grant select, insert, update, delete on public.sites         to service_role;
grant select, insert, update, delete on public.records       to service_role;
grant select, insert, update, delete on public.record_photos to service_role;
grant usage, select on sequence public.sites_id_seq          to service_role;
grant usage, select on sequence public.records_id_seq        to service_role;
grant usage, select on sequence public.record_photos_id_seq  to service_role;
