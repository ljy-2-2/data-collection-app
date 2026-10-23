"""Optional geometry operation; baseline official-math source is unchanged."""
from teleop.robots.kuavo5_numeric_source import SOURCE


def geometry_source():
    marker = '    if(message.at("op")=="fk") {'
    if SOURCE.count(marker) != 1:
        raise ValueError('numeric source extension point changed; review required')
    extension = r'''
    if(message.at("op")=="geometry") {
      const auto raw=vector_field(message.at("q"),14);
      Eigen::VectorXd q(14);for(int i=0;i<14;++i)q[i]=raw[i];
      auto& context=plant_->GetMyMutableContextFromRoot(context_.get());
      plant_->SetPositions(&context,q);
      Json arms=Json::object();
      for(int side=0;side<2;++side) {
        const std::string prefix=side==0?"zarm_l":"zarm_r";
        Json arm=Json::object();
        for(const auto& pair:std::vector<std::pair<std::string,std::string>>{
              {"shoulder",prefix+"1_link"},{"elbow",prefix+"4_link"},
              {"wrist",prefix+"6_link"},{"hand",side==0?"eef_left":"eef_right"}}) {
          arm[pair.first]=numbers(plant_->GetFrameByName(pair.second).CalcPoseInWorld(context).translation());
        }
        arms[side==0?"left":"right"]=arm;
      }
      return {{"frame_id","base_link"},{"arms",arms}};
    }
'''
    return SOURCE.replace(marker, extension+marker)


def numeric_source_for(arm_posture):
    if arm_posture == 'baseline':
        return SOURCE
    if arm_posture == 'elbow_preference':
        return geometry_source()
    if arm_posture == 'elbow_preference_v2':
        from teleop.robots.kuavo5_refinement_source import retain_successful_source
        return retain_successful_source(geometry_source())
    raise ValueError('unknown arm posture method')
