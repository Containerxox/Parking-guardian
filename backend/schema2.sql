CREATE TABLE buildings(
  id integer primary key autoincrement,
  name text not null,
  address text not null,
  unique(name, address)
);
CREATE TABLE users(
  id integer primary key autoincrement,
  user_id text not null unique,
  password_hash text not null,
  role text not null default 'user' check(role in('admin', 'user')),
  building_id integer,
  foreign key(building_id) references buildings(id) on delete cascade
);
CREATE TABLE machine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  machine_id TEXT NOT NULL UNIQUE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (username) REFERENCES users(user_id)
    ON DELETE CASCADE
    ON UPDATE CASCADE
);