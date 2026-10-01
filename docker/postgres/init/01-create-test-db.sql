-- Separate database for the automated tests, so they never touch development data.
-- Tests drop and recreate every table for each test case.
CREATE DATABASE parking_guardian_test;
