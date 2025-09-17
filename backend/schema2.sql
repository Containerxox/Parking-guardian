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
