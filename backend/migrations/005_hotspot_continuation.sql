-- 跨小时疏导单接续：一张疏导单可在多个楼层小时被热区引用。
-- dispatch_tasks.hotspot_id 仍保留该单首次下发时的原热区；本表记录每个小时
-- 实际接到该单的热区（含原热区回填），使两个相邻小时都能查到同一张原单。
CREATE TABLE IF NOT EXISTS hotspot_task_links (
    id INT PRIMARY KEY AUTO_INCREMENT,
    hotspot_id INT NOT NULL,
    task_id INT NOT NULL,
    hour_id INT NOT NULL,
    origin TINYINT(1) NOT NULL DEFAULT 0,
    linked_by INT NOT NULL,
    linked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_link_hotspot (hotspot_id),
    UNIQUE KEY uq_link_task_hour_identity (task_id, hotspot_id),
    -- 同一张单在同一个楼层小时只允许接到一片热区（拆分/合并也不例外）。
    UNIQUE KEY uq_link_task_per_hour (task_id, hour_id),
    INDEX idx_link_task (task_id),
    FOREIGN KEY (hotspot_id) REFERENCES hotspots(id),
    FOREIGN KEY (task_id) REFERENCES dispatch_tasks(id),
    FOREIGN KEY (hour_id) REFERENCES floor_hours(id),
    FOREIGN KEY (linked_by) REFERENCES users(id)
) ENGINE=InnoDB;

-- 旧数据卷幂等回填：每张单的原热区作为 origin 关联，已存在则跳过。
INSERT IGNORE INTO hotspot_task_links(hotspot_id, task_id, hour_id, origin, linked_by)
SELECT t.hotspot_id, t.id, h.hour_id, 1, COALESCE(h.confirmed_by, 1)
FROM dispatch_tasks t JOIN hotspots h ON h.id = t.hotspot_id
WHERE NOT EXISTS (
    SELECT 1 FROM hotspot_task_links l
    WHERE l.hotspot_id = t.hotspot_id AND l.task_id = t.id
);
