CREATE TABLE IF NOT EXISTS "users"(
  "id"	integer,
  "user_id"	text NOT NULL UNIQUE,
  "password_hash"	text NOT NULL,
  role text not null default 'user' check(role in('admin', 'user')),
  PRIMARY KEY("id" AUTOINCREMENT)
);
