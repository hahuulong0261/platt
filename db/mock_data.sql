PRAGMA foreign_keys = ON;

BEGIN TRANSACTION;

-- Tất cả tài khoản mock dùng mật khẩu: 123456
-- Ánh xạ người giám sát:
--   A1 = users.id 1, A2 = users.id 2, A3 = users.id 3
-- Ánh xạ người bị giám sát:
--   M1 = users.id 4, M2 = users.id 5, M3 = users.id 6
INSERT INTO users (id, name, phone, password_hash, role) VALUES
    (1, 'A1', '0900000001', 'scrypt:32768:8:1$1qLhHkBWx8HkcCRM$74bde7c1d54b775b8f2afa6af1f8edfb031311a46a1f3b135ee01aca63ab7352cac3452a9058a2db33560399296c0239950b5a0a2e5038130a9a915601bc4ce5', 'supervisor'),
    (2, 'A2', '0900000002', 'scrypt:32768:8:1$1qLhHkBWx8HkcCRM$74bde7c1d54b775b8f2afa6af1f8edfb031311a46a1f3b135ee01aca63ab7352cac3452a9058a2db33560399296c0239950b5a0a2e5038130a9a915601bc4ce5', 'supervisor'),
    (3, 'A3', '0900000003', 'scrypt:32768:8:1$1qLhHkBWx8HkcCRM$74bde7c1d54b775b8f2afa6af1f8edfb031311a46a1f3b135ee01aca63ab7352cac3452a9058a2db33560399296c0239950b5a0a2e5038130a9a915601bc4ce5', 'supervisor'),
    (4, 'M1', '0910000001', 'scrypt:32768:8:1$1qLhHkBWx8HkcCRM$74bde7c1d54b775b8f2afa6af1f8edfb031311a46a1f3b135ee01aca63ab7352cac3452a9058a2db33560399296c0239950b5a0a2e5038130a9a915601bc4ce5', 'monitored'),
    (5, 'M2', '0910000002', 'scrypt:32768:8:1$1qLhHkBWx8HkcCRM$74bde7c1d54b775b8f2afa6af1f8edfb031311a46a1f3b135ee01aca63ab7352cac3452a9058a2db33560399296c0239950b5a0a2e5038130a9a915601bc4ce5', 'monitored'),
    (6, 'M3', '0910000003', 'scrypt:32768:8:1$1qLhHkBWx8HkcCRM$74bde7c1d54b775b8f2afa6af1f8edfb031311a46a1f3b135ee01aca63ab7352cac3452a9058a2db33560399296c0239950b5a0a2e5038130a9a915601bc4ce5', 'monitored');

-- Token gốc dùng khi demo:
--   C1: camera-c1-token
--   C2: camera-c2-token
--   C3: camera-c3-token
-- Database chỉ lưu SHA-256 của token, không lưu token gốc.
INSERT INTO cameras (id, name, token_hash) VALUES
    (1, 'C1', '40d93a8a778c3da11cf8f5fc18c36273de45d68a9be7728bf9420ffd252c66ac'),
    (2, 'C2', '246b1979fa05b5368b6ab35239105ebd28dd980de93525b7fae57502d8085655'),
    (3, 'C3', '0ce55eea7ab0866d6572a6aeef1f87bb5613dce6d59e6efff010625a4560c5ea');

-- A1 giám sát C1, C2 và C3; A2 giám sát C2; A3 chưa giám sát camera.
INSERT INTO supervisor_cameras (supervisor_id, camera_id) VALUES
    (1, 1),
    (1, 2),
    (1, 3),
    (2, 2);

-- Chưa tạo supervisor_monitored vì chưa có yêu cầu gán A1/A2/A3 cho M1/M2/M3.

COMMIT;
